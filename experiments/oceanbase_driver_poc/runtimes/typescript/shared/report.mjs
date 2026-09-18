/**
 * Rendering of run reports, mirroring `common/report.py`.
 *
 * Same rule as everywhere else: an unexecuted check is written as unverified, never
 * as a pass.
 */

import { STATUS_LABEL } from './results.mjs';

/**
 * Render a compact terminal table of every check across several reports.
 *
 * @param {object[]} reports
 * @returns {string}
 */
export function renderTable(reports) {
  const lines = ['模式      检查            状态                说明'];
  for (const report of reports) {
    for (const check of report.checks) {
      lines.push(
        `${report.mode.padEnd(8)} ${check.check_id.padEnd(14)} ${(STATUS_LABEL[check.status] ?? check.status).padEnd(18)} ${oneline(check.summary)}`,
      );
    }
  }
  return lines.join('\n');
}

/**
 * Render the per-mode aggregate verdicts.
 *
 * @param {object[]} reports
 * @returns {string}
 */
export function renderSummary(reports) {
  const lines = [];
  for (const report of reports) {
    const agg = report.aggregate;
    lines.push(
      `[${report.mode}] 驱动 ${report.driver?.name} ${report.driver?.version} | ${agg.verdict}（${agg.passed}/${agg.total} 通过，${agg.not_executed} 未执行）`,
    );
    if (report.missing_env?.length) lines.push(`  缺少环境变量：${report.missing_env.join(', ')}`);
  }
  return lines.join('\n');
}

/**
 * Render one mode's results as a Markdown fragment.
 *
 * @param {object} report
 * @returns {string}
 */
export function renderMarkdown(report) {
  const lines = [`## ${report.mode} Mode（TypeScript / node）`, ''];
  lines.push(`- Driver：\`${report.driver?.name}\``);
  lines.push(`- Driver 版本：\`${report.driver?.version}\``);
  lines.push(`- Runtime：\`${report.runtime}\``);
  lines.push(`- 汇总结论：**${report.aggregate.verdict}**`);
  lines.push('');
  lines.push('| 检查 | 项目 | 状态 | 说明 |');
  lines.push('|---|---|---|---|');
  for (const check of report.checks) {
    lines.push(`| ${check.check_id} | ${check.name} | ${STATUS_LABEL[check.status] ?? check.status} | ${oneline(check.summary)} |`);
  }
  lines.push('');
  const pending = report.checks.filter((c) => c.status === 'SKIP_NO_ENV').map((c) => c.check_id);
  if (pending.length) lines.push(`> 未验证项：${pending.join(', ')}（缺环境，不得据本表宣称通过）`);
  return lines.join('\n');
}

function oneline(text) {
  return String(text ?? '').split(/\s+/).filter(Boolean).join(' ').replace(/\|/g, '\\|');
}
