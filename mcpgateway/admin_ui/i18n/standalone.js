import dictionary from './zh-CN.js';
import { makeTranslator } from './translator.js';
import { installOverlay } from './apply.js';

function start() {
  let useEnglish = false;
  try {
    useEnglish = localStorage.getItem('mcpgateway.locale') === 'en';
  } catch (_) {
    // Storage may be unavailable in a private window.
  }
  if (useEnglish) {
    document.documentElement.setAttribute('data-i18n-ready', '');
    return;
  }
  try {
    installOverlay({ translate: makeTranslator(dictionary) });
  } catch (error) {
    console.error('Admin UI translation failed', error);
    document.documentElement.setAttribute('data-i18n-ready', '');
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start, { once: true });
} else {
  start();
}
