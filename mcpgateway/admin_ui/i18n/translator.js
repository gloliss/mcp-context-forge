export function normalize(value) {
  return value.replace(/\s+/g, ' ').trim();
}

// 确认对话框里的实体类型小写名（ENTITY_DISPLAY_NAMES 的取值）→ 中文/规范展示。
// 与词典一致：MCP 领域名词（Tool/Resource/Prompt/Gateway/Server/Root/Agent）保留英文，
// 平台概念（Team/User）与 model/provider/token 等按词典翻译。
const ENTITY_DISPLAY_ZH = {
  tool: 'Tool',
  resource: 'Resource',
  prompt: 'Prompt',
  gateway: 'Gateway',
  server: 'Server',
  agent: 'Agent',
  team: '团队',
  user: '用户',
  root: 'Root',
};

function displayZh(name) {
  return ENTITY_DISPLAY_ZH[name] || name;
}

export const RULES = [
  [/^Deleted (\d+) tools?$/i, (match) => `已删除 ${match[1]} 个 Tools`],
  [/^(\d+) items? selected$/i, (match) => `已选中 ${match[1]} 项`],
  [/^Enter a new password\. Minimum length: (\d+)\.$/, (match) => `请输入新密码，至少 ${match[1]} 个字符。`],
  // —— 确认对话框（含插值变量）——
  [/^Request to join "(.+)"\?$/, (match) => `确定要申请加入「${match[1]}」吗？`],
  [/^Leave team "(.+)"\? You will lose access to team resources\.$/, (match) => `确定要退出团队「${match[1]}」吗？您将失去对该团队资源的访问权限。`],
  [/^Delete team "(.+)"\? This action cannot be undone\.$/, (match) => `确定要删除团队「${match[1]}」吗？此操作不可撤销。`],
  [/^Delete database source "(.+)"\? This cannot be undone\.$/, (match) => `确定要删除数据库源「${match[1]}」吗？此操作不可撤销。`],
  [/^Are you sure you want to delete the model "(.+)"\?$/, (match) => `确定要删除模型「${match[1]}」吗？`],
  [/^Are you sure you want to delete the provider "(.+)"\? This will also delete all associated models\.$/, (match) => `确定要删除提供方「${match[1]}」吗？其关联的所有模型也将被删除。`],
  [/^Are you sure you want to revoke the token "(.+)"\? This action cannot be undone\.$/, (match) => `确定要吊销令牌「${match[1]}」吗？此操作不可撤销。`],
  [/^Are you sure you want to leave the team "(.+)"\? This action cannot be undone\.$/, (match) => `确定要退出团队「${match[1]}」吗？此操作不可撤销。`],
  [/^Are you sure you want to permanently delete this (.+)\? \(Deactivation is reversible, deletion is permanent\)$/, (match) => `确定要永久删除该 ${displayZh(match[1])} 吗？（停用可逆，删除不可逆）`],
  [/^Are you sure you want to permanently delete (.+) "(.+)"\? \(Deactivation is reversible, deletion is permanent\)$/, (match) => `确定要永久删除 ${displayZh(match[1])}「${match[2]}」吗？（停用可逆，删除不可逆）`],
  [/^Also purge ALL metrics history for this (.+)\? This deletes raw metrics and hourly rollups and cannot be undone\.$/, (match) => `同时清除该 ${displayZh(match[1])} 的全部指标历史记录吗？这将删除原始指标与小时级汇总，且不可撤销。`],
  [/^Also purge ALL metrics history for (.+) "(.+)"\? This deletes raw metrics and hourly rollups and cannot be undone\.$/, (match) => `同时清除 ${displayZh(match[1])}「${match[2]}」的全部指标历史记录吗？这将删除原始指标与小时级汇总，且不可撤销。`],
  // —— 成员邀请/角色分配的失败提示（前缀固定，后缀为服务器返回的错误消息）——
  [/^Error adding member: (.+)$/, (match) => `添加成员时发生错误：${match[1]}`],
  [/^Error assigning role: (.+)$/, (match) => `分配角色时发生错误：${match[1]}`],
];

export function makeTranslator(dictionary, rules = RULES) {
  return (value) => {
    if (!value || !value.trim()) return value;
    const key = normalize(value);
    const leading = value.match(/^\s*/)[0];
    const trailing = value.match(/\s*$/)[0];
    const translation = Object.prototype.hasOwnProperty.call(dictionary, key) ? dictionary[key] : null;
    if (translation !== null) return leading + translation + trailing;
    for (const [pattern, render] of rules) {
      const match = key.match(pattern);
      if (match) return leading + render(match) + trailing;
    }
    return value;
  };
}
