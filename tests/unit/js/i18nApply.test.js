import { afterEach, describe, expect, it } from 'vitest';
import { installOverlay } from '../../../mcpgateway/admin_ui/i18n/apply.js';
import { makeTranslator } from '../../../mcpgateway/admin_ui/i18n/translator.js';

const translate = makeTranslator({ Name: '名称', Cancel: '取消', Search: '搜索', 'ContextForge - Gateway Administration': 'ContextForge - Gateway 管理' });
let overlay;

afterEach(() => {
  overlay?.uninstall();
  overlay = undefined;
  document.body.replaceChildren();
  document.documentElement.removeAttribute('data-i18n-ready');
});

describe('admin UI DOM overlay', () => {
  it('translates text, title and safe attributes without touching form values', () => {
    document.body.innerHTML = '<section><span>Name</span><input placeholder="Search" value="Name"><button aria-label="Cancel" title="Search">Cancel</button></section>';
    overlay = installOverlay({ translate });
    expect(document.querySelector('span').textContent).toBe('名称');
    expect(document.querySelector('input').placeholder).toBe('搜索');
    expect(document.querySelector('input').value).toBe('Name');
    expect(document.querySelector('button').getAttribute('aria-label')).toBe('取消');
    expect(document.querySelector('button').title).toBe('搜索');
    expect(document.documentElement.lang).toBe('zh-CN');
    expect(document.documentElement.hasAttribute('data-i18n-ready')).toBe(true);
    overlay.flushNow();
    expect(document.querySelector('span').textContent).toBe('名称');
  });

  it('skips code, textareas, contenteditable and explicit exclusions', () => {
    document.body.innerHTML = '<div><code>Name</code><textarea>Name</textarea><div contenteditable="true">Name</div><div data-i18n-skip>Name</div><div class="CodeMirror">Name</div><span>Name</span></div>';
    overlay = installOverlay({ translate });
    for (const selector of ['code', 'textarea', '[contenteditable]', '[data-i18n-skip]', '.CodeMirror']) {
      expect(document.querySelector(selector).textContent).toBe('Name');
    }
    expect(document.querySelector('span').textContent).toBe('名称');
  });

  it('does not translate a newly inserted skipped subtree', async () => {
    document.body.innerHTML = '<div id="host"></div>';
    overlay = installOverlay({ translate });
    const code = document.createElement('code');
    code.textContent = 'Name';
    document.querySelector('#host').append(code);
    await Promise.resolve();
    overlay.flushNow();
    expect(code.textContent).toBe('Name');
  });

  it('handles appended nodes and updates without feedback loops', async () => {
    document.body.innerHTML = '<div id="target">Name</div>';
    overlay = installOverlay({ translate });
    const target = document.querySelector('#target');
    target.append(document.createTextNode('Cancel'));
    await Promise.resolve();
    overlay.flushNow();
    expect(target.textContent).toBe('名称取消');
    target.firstChild.nodeValue = 'Name';
    await Promise.resolve();
    overlay.flushNow();
    expect(target.firstChild.nodeValue).toBe('名称');
    await Promise.resolve();
    overlay.flushNow();
    expect(target.textContent).toBe('名称取消');
  });
});
