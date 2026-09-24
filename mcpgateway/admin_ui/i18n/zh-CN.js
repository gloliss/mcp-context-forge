/**
 * ContextForge Admin UI 词典 —— 中文（简体）汇总入口
 *
 * 设计：本词典用于 admin_ui/i18n 下的运行时文字覆盖层（DOM 文本节点精确匹配），
 * 不修改任何模板或 JS 源码，因此 key 必须是界面上逐字出现的英文原文
 * （已 trim、已把连续空白折叠为单个半角空格），不得增删标点或改写大小写。
 *
 * 分域文件位于 ./dict/ 下，键集合互不重叠（重复键会在单测中失败）：
 *   - common.js	通用界面词（导航、按钮、标题、空状态）
 *   - forms.js	表单域（字段标签、placeholder、帮助文字）
 *   - tables.js	表格列名与状态值
 *   - toasts.js	轻提示与操作反馈
 *   - errors.js	错误、警告、校验与权限提示
 *   - auth.js	登录与密码页、邀请/重置邮件
 *   - metrics.js	指标、性能与可观测性
 *   - admin.js	平台管理（团队/用户/令牌/插件/数据库源等）
 *
 * 术语边界：MCP / Tool / Prompt / Resource / Server / Root / Plugin / Registry /
 * A2A / Gateway / Agent / SQL / HTTP / gRPC / API / URL / URI / JSON / YAML /
 * JWT / OAuth 等专有名词保留英文原文，仅翻译其周边的界面词。
 */

import common from "./dict/common.js";
import forms from "./dict/forms.js";
import tables from "./dict/tables.js";
import toasts from "./dict/toasts.js";
import errors from "./dict/errors.js";
import auth from "./dict/auth.js";
import metrics from "./dict/metrics.js";
import admin from "./dict/admin.js";

/** 按域划分的词典，域内键无重复。 */
export const dictionaries = {
  common,
  forms,
  tables,
  toasts,
  errors,
  auth,
  metrics,
  admin,
};



/** 合并后的完整词典：英文原文 -> 简体中文。 */
const zhCN = {
  ...common,
  ...forms,
  ...tables,
  ...toasts,
  ...errors,
  ...auth,
  ...metrics,
  ...admin,
};

export default zhCN;
