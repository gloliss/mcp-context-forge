const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE', 'KBD', 'SAMP', 'TEXTAREA', 'NOSCRIPT', 'TEMPLATE']);
const TEXT_ATTRIBUTES = ['placeholder', 'title', 'aria-label', 'alt'];
const OBSERVER_OPTIONS = {
  subtree: true,
  childList: true,
  characterData: true,
  attributes: true,
  attributeFilter: TEXT_ATTRIBUTES,
};

export function installOverlay({ root = document.body, translate }) {
  const doc = root?.ownerDocument || document;
  const writtenText = new WeakMap();
  const writtenAttrs = new WeakMap();
  const dirty = new Set();
  let observer;
  let scheduled = false;
  let timer;
  let frame;
  let active = true;

  function skipped(element) {
    return SKIP_TAGS.has(element.tagName) || element.hasAttribute('data-i18n-skip') ||
      element.hasAttribute('contenteditable') || element.isContentEditable || element.closest('.CodeMirror') !== null;
  }

  function visit(element) {
    if (element.nodeType === 3) {
      const current = element.nodeValue;
      if (!current || writtenText.get(element) === current) return;
      const translated = translate(current);
      if (translated !== current) {
        writtenText.set(element, translated);
        element.nodeValue = translated;
      } else {
        writtenText.delete(element);
      }
      return;
    }
    if (element.nodeType !== 1 || skipped(element)) return;
    const previous = writtenAttrs.get(element) || new Map();
    for (const name of TEXT_ATTRIBUTES) {
      if (!element.hasAttribute(name)) continue;
      const current = element.getAttribute(name);
      if (previous.get(name) === current) continue;
      const translated = translate(current);
      if (translated !== current) {
        previous.set(name, translated);
        element.setAttribute(name, translated);
      } else {
        previous.delete(name);
      }
    }
    writtenAttrs.set(element, previous);
  }

  function pass(node) {
    if (!node || !node.isConnected) return;
    if (node.nodeType === 1 && skipped(node)) return;
    const ancestor = node.parentElement;
    if (ancestor?.closest('script, style, code, pre, kbd, samp, textarea, noscript, template, [data-i18n-skip], [contenteditable], .CodeMirror')) return;
    const filter = {
      acceptNode(current) {
        if (current.nodeType === 1 && skipped(current)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    };
    const walker = doc.createTreeWalker(node, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, filter);
    if (filter.acceptNode(node) !== NodeFilter.FILTER_REJECT) visit(node);
    while (walker.nextNode()) visit(walker.currentNode);
  }

  function enqueue(records) {
    for (const record of records) {
      if (record.type === 'childList') {
        for (const node of record.addedNodes) dirty.add(node);
      } else {
        dirty.add(record.target);
      }
    }
    if (dirty.size) schedule();
  }

  function flushNow() {
    if (!active || !root) return;
    const records = observer.takeRecords();
    if (!dirty.size && !records.length) return;
    if (doc.hidden) return;
    observer.disconnect();
    for (const record of records) {
      if (record.type === 'childList') {
        for (const node of record.addedNodes) dirty.add(node);
      } else dirty.add(record.target);
    }
    const nodes = Array.from(dirty);
    dirty.clear();
    try {
      if (nodes.length > 400) pass(root);
      else for (const node of nodes) pass(node);
    } finally {
      if (active) observer.observe(root, OBSERVER_OPTIONS);
    }
  }

  function schedule() {
    if (scheduled || doc.hidden) return;
    scheduled = true;
    const run = () => {
      if (!scheduled) return;
      scheduled = false;
      clearTimeout(timer);
      flushNow();
    };
    frame = doc.defaultView?.requestAnimationFrame?.(run);
    timer = setTimeout(run, 250);
  }

  function onVisible() {
    if (!doc.hidden && dirty.size) schedule();
  }

  try {
    if (root) {
      pass(root);
      observer = new MutationObserver(enqueue);
      observer.observe(root, OBSERVER_OPTIONS);
      doc.addEventListener('visibilitychange', onVisible);
    }
    doc.title = translate(doc.title);
    doc.documentElement.lang = 'zh-CN';
  } finally {
    doc.documentElement.setAttribute('data-i18n-ready', '');
  }

  return {
    flushNow,
    uninstall() {
      active = false;
      observer?.disconnect();
      doc.removeEventListener('visibilitychange', onVisible);
      if (frame !== undefined) doc.defaultView?.cancelAnimationFrame?.(frame);
      clearTimeout(timer);
      dirty.clear();
    },
  };
}
