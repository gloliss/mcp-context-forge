import { describe, expect, it } from 'vitest';
import { makeTranslator, normalize } from '../../../mcpgateway/admin_ui/i18n/translator.js';

describe('admin UI translator', () => {
  it('normalizes whitespace without changing misses', () => {
    expect(normalize('  Save\t Changes\n')).toBe('Save Changes');
    const translate = makeTranslator({ 'Save Changes': '保存修改' });
    expect(translate('  Save\t Changes\n')).toBe('  保存修改\n');
    const original = '  Unknown field  ';
    expect(translate(original)).toBe(original);
    expect(translate('   ')).toBe('   ');
  });

  it('keeps domain names and handles variable text', () => {
    const translate = makeTranslator({ 'Save Changes': '保存修改' });
    expect(translate('Tool')).toBe('Tool');
    expect(translate('Deleted 3 tools')).toBe('已删除 3 个 Tools');
    expect(translate('Enter a new password. Minimum length: 12.')).toBe('请输入新密码，至少 12 个字符。');
  });
});
