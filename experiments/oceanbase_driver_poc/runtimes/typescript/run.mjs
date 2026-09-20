#!/usr/bin/env node
/**
 * Executable entry point for the TypeScript side of the OceanBase driver POC.
 *
 *     node experiments/oceanbase_driver_poc/runtimes/typescript/run.mjs --mode all
 *
 * Exit status mirrors `run_poc.py`: 0 when nothing failed, 2 when a check failed,
 * errored, or was indeterminate, 1 for a usage problem. A mode with no configured
 * environment reports SKIP_NO_ENV and does not by itself make the run unclean.
 *
 * Results use the same schema and the same six-status vocabulary as the Python
 * runner, so the two are directly comparable and can be read side by side.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { loadConfig, missingRequired, buildRedactor, redactedTarget } from './shared/config.mjs';
import { renderMarkdown, renderSummary, renderTable } from './shared/report.mjs';
import { ABSTAINED_STATUSES, UNCLEAN_STATUSES, runReport } from './shared/results.mjs';
import { DRIVER_PACKAGE as MYSQL_PACKAGE, runMysqlChecks } from './checks/mysql.mjs';
import { DRIVER_PACKAGE as ORACLE_PACKAGE, load as loadOracle, runOracleChecks, runPreflightChecks } from './checks/oracle.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const MODES = ['mysql', 'oracle'];

const EXIT_OK = 0;
const EXIT_USAGE = 1;
const EXIT_UNHEALTHY = 2;

function parseArgs(argv) {
  // Defaults into the POC's shared results directory rather than a local one, so the
  // established ignore rule covers it and the two runtimes' outputs sit together.
  const args = { mode: 'all', runtime: 'node', out: join(HERE, '..', '..', 'results', 'ts'), printJson: false, failOnSkip: false, mdOut: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--mode') args.mode = argv[++i];
    else if (arg === '--runtime') args.runtime = argv[++i];
    else if (arg === '--out') args.out = argv[++i];
    else if (arg === '--md-out') args.mdOut = argv[++i];
    else if (arg === '--print-json') args.printJson = true;
    else if (arg === '--fail-on-skip') args.failOnSkip = true;
    else if (arg === '-h' || arg === '--help') args.help = true;
    else throw new Error(`未知参数：${arg}`);
  }
  return args;
}

function usage() {
  return [
    '用法：node run.mjs [--mode mysql|oracle|all] [--runtime <label>] [--out <dir>]',
    '                [--md-out <file>] [--print-json] [--fail-on-skip]',
    '',
    '环境变量与 Python 侧完全一致（OB_MYSQL_* / OB_ORACLE_*），同一套环境驱动两个运行时。',
  ].join('\n');
}

/**
 * Read the driver version, from package metadata rather than a hard-coded string.
 *
 * @param {string} name
 * @returns {Promise<string>}
 */
async function packageVersion(name) {
  try {
    const { createRequire } = await import('node:module');
    const require = createRequire(import.meta.url);
    return require(`${name}/package.json`).version;
  } catch {
    return 'unknown';
  }
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(String(error.message));
    console.error(usage());
    return EXIT_USAGE;
  }
  if (args.help) {
    console.log(usage());
    return EXIT_OK;
  }
  if (args.mode !== 'all' && !MODES.includes(args.mode)) {
    console.error(`未知模式：${args.mode}`);
    return EXIT_USAGE;
  }

  const modes = args.mode === 'all' ? MODES : [args.mode];
  const redactor = buildRedactor(MODES);
  const startedAt = new Date().toISOString();

  const mysqlVersion = await packageVersion('mysql2');
  const oracleLoaded = await loadOracle();
  const oracleVersion = oracleLoaded.version ?? (await packageVersion('oracledb'));

  const reports = [];
  for (const mode of modes) {
    const config = loadConfig(mode);
    const missingEnv = missingRequired(mode);
    const target = config ? redactedTarget(config, redactor) : { configured: false };

    let checks;
    let driver;
    if (mode === 'mysql') {
      driver = { name: MYSQL_PACKAGE, version: mysqlVersion, installed: true };
      checks = await runMysqlChecks(config, missingEnv, redactor);
    } else {
      driver = { name: ORACLE_PACKAGE, version: oracleVersion, installed: oracleLoaded.module !== null };
      const preflight = runPreflightChecks(oracleLoaded);
      const connection = await runOracleChecks(config, missingEnv, oracleLoaded.module, redactor);
      // Preflight last: probing thick mode switches the process and cannot be undone,
      // so a thin connection result must be obtained before it runs.
      checks = [...connection, ...preflight];
    }

    reports.push(
      runReport({
        mode,
        runtime: args.runtime,
        driver: redactor.redactDeep(driver),
        target,
        checks,
        startedAt,
        finishedAt: new Date().toISOString(),
        missingEnv,
      }),
    );
  }

  mkdirSync(args.out, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  const written = [];
  for (const report of reports) {
    const path = join(args.out, `${report.mode}-${args.runtime}-${stamp}.json`);
    writeFileSync(path, JSON.stringify(redactor.redactDeep(report), null, 2), 'utf-8');
    written.push(path);
  }
  for (const path of written) console.log(`结果已写入 ${path}`);

  console.log('');
  console.log(renderTable(reports));
  console.log('');
  console.log(renderSummary(reports));

  if (args.mdOut) {
    writeFileSync(args.mdOut, reports.map(renderMarkdown).join('\n\n'), 'utf-8');
    console.log(`\n结论文档段落已写入 ${args.mdOut}`);
  }
  if (args.printJson) console.log(JSON.stringify(redactor.redactDeep(reports), null, 2));

  const checks = reports.flatMap((r) => r.checks);
  const unclean = checks.filter((c) => UNCLEAN_STATUSES.includes(c.status));
  const abstained = checks.filter((c) => ABSTAINED_STATUSES.includes(c.status));

  if (unclean.length > 0) {
    console.error(`\n有 ${unclean.length} 项检查失败、异常或无法判定。`);
    return EXIT_UNHEALTHY;
  }
  if (abstained.length > 0 && args.failOnSkip) {
    console.error(`\n有 ${abstained.length} 项未执行或未被驱动支持。`);
    return EXIT_UNHEALTHY;
  }
  if (abstained.length > 0) {
    console.log(`\n注意：${abstained.length} 项未执行或未被驱动支持，未验证项不得据本表宣称通过。`);
  }
  return EXIT_OK;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`执行失败：${error?.stack ?? error}`);
    process.exit(EXIT_USAGE);
  });
