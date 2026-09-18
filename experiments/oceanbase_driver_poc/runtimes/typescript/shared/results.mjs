/**
 * The result contract, mirroring `common/results.py` on the Python side.
 *
 * The TypeScript evaluation is only useful if its results can be read the same way
 * as the Python ones, so the status vocabulary and the verdict wording are copied
 * deliberately rather than reinvented. In particular the rule that matters is the
 * same: an unexecuted check is never reported as a pass.
 */

export const CheckStatus = Object.freeze({
  PASS: 'PASS',
  FAIL: 'FAIL',
  SKIP_NO_ENV: 'SKIP_NO_ENV',
  UNSUPPORTED: 'UNSUPPORTED',
  INDETERMINATE: 'INDETERMINATE',
  ERROR: 'ERROR',
});

/** Ran and did not pass, or could not be judged. Unclean by default. */
export const UNCLEAN_STATUSES = [CheckStatus.FAIL, CheckStatus.ERROR, CheckStatus.INDETERMINATE];

/** Nothing established, nothing failed either. Fatal only with --fail-on-skip. */
export const ABSTAINED_STATUSES = [CheckStatus.SKIP_NO_ENV, CheckStatus.UNSUPPORTED];

/** Human labels, matching the Python renderer so the two read alike. */
export const STATUS_LABEL = Object.freeze({
  [CheckStatus.PASS]: '通过',
  [CheckStatus.FAIL]: '失败',
  [CheckStatus.SKIP_NO_ENV]: '未验证（缺环境）',
  [CheckStatus.UNSUPPORTED]: '驱动不支持',
  [CheckStatus.INDETERMINATE]: '无法判定',
  [CheckStatus.ERROR]: '执行异常',
});

/**
 * Whether a status may be reported as a pass.
 *
 * @param {string} status
 * @returns {boolean} True only for PASS.
 */
export function countsAsPass(status) {
  return status === CheckStatus.PASS;
}

/**
 * Summarise checks without ever promoting a non-PASS state.
 *
 * @param {Array<{status: string}>} checks
 * @returns {{counts: Record<string, number>, total: number, passed: number, not_executed: number, all_passed: boolean, has_skips: boolean, verdict: string}}
 */
export function aggregate(checks) {
  const counts = {};
  for (const status of Object.values(CheckStatus)) counts[status] = 0;
  for (const check of checks) counts[check.status] = (counts[check.status] ?? 0) + 1;

  const total = checks.length;
  const passed = counts[CheckStatus.PASS];
  const skipped = counts[CheckStatus.SKIP_NO_ENV];

  return {
    counts,
    total,
    passed,
    not_executed: skipped,
    all_passed: total > 0 && passed === total,
    has_skips: skipped > 0,
    verdict: verdict(counts, total),
  };
}

/**
 * Phrase an outcome so no non-PASS state reads as a pass.
 *
 * @param {Record<string, number>} counts
 * @param {number} total
 * @returns {string}
 */
function verdict(counts, total) {
  if (total === 0) return '无检查项';
  if (counts[CheckStatus.PASS] === total) return '全部通过';
  if (counts[CheckStatus.SKIP_NO_ENV] === total) return '未验证（缺环境）';

  const labels = [
    ['项通过', CheckStatus.PASS],
    ['项失败', CheckStatus.FAIL],
    ['项驱动不支持', CheckStatus.UNSUPPORTED],
    ['项无法判定', CheckStatus.INDETERMINATE],
    ['项执行异常', CheckStatus.ERROR],
    ['项未执行（缺环境）', CheckStatus.SKIP_NO_ENV],
  ];
  return labels
    .filter(([, status]) => counts[status])
    .map(([label, status]) => `${counts[status]} ${label}`)
    .join('，');
}

/**
 * Build one check result.
 *
 * @param {object} spec
 * @param {string} spec.checkId
 * @param {string} spec.mode
 * @param {string} spec.name
 * @param {string} spec.status
 * @param {string} spec.summary
 * @param {object} [spec.evidence]
 * @returns {object}
 */
export function checkResult({ checkId, mode, name, status, summary, evidence = {} }) {
  return { check_id: checkId, mode, name, status, summary, evidence };
}

/**
 * Build a per-mode report.
 *
 * @param {object} spec
 * @returns {object}
 */
export function runReport({ mode, runtime, driver, target, checks, startedAt, finishedAt, missingEnv = [] }) {
  return {
    schema_version: 1,
    mode,
    runtime,
    driver,
    target,
    missing_env: missingEnv,
    started_at: startedAt,
    finished_at: finishedAt,
    aggregate: aggregate(checks),
    checks,
  };
}
