// ===================================================================
// IN-APP DIALOGS — window.confirm() / window.alert() replacements
// ===================================================================
//
// The admin UI previously used the browser-native confirm()/alert(), which
// bypass the i18n overlay and the theme. These helpers render in-page dialogs
// instead, so:
//
//   * the message becomes a DOM text node → the i18n MutationObserver (see
//     i18n/apply.js) translates it when it is a dictionary key or matches a
//     translator RULE;
//   * the buttons carry dictionary keys ("Confirm" / "Cancel" / "OK") that
//     the overlay translates the same way;
//   * the dialogs inherit the theme-token classes, so they render correctly
//     in both light and dark mode.
//
// Safety constraints (see security.js installInnerHtmlGuard):
//   * every node is built with createElement + textContent — never innerHTML —
//     so the innerHTML setter guard is never triggered;
//   * no inline event handlers (onclick=...), no <style> element, no eval;
//   * exactly one dialog at a time (concurrent calls are queued);
//   * falls back to the native window.confirm()/window.alert() when the
//     document is unavailable or rendering throws, so call sites never need
//     their own fallback branch.
// ===================================================================

let queue = Promise.resolve();

/** Serialize dialogs so at most one is open at a time. */
function enqueue(task) {
  const result = queue.then(task, task);
  // Keep the queue alive even if a dialog task rejects.
  queue = result.then(() => {}, () => {});
  return result;
}

const DIALOG_BUTTON_STYLES = {
  danger: "bg-red-600 hover:bg-red-700 text-white focus:ring-red-500",
  primary: "bg-indigo-600 hover:bg-indigo-700 text-white focus:ring-indigo-500",
  secondary:
    "bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300 border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-600 focus:ring-indigo-500",
};

const DIALOG_BUTTON_BASE =
  "inline-flex items-center justify-center px-4 py-2 text-sm font-medium rounded-md focus:outline-none focus:ring-2 focus:ring-offset-2 transition-colors";

function canRender() {
  return typeof document !== "undefined" && document.body !== null;
}

/**
 * Open an in-page modal dialog.
 *
 * @param {string} message - The message to display (rendered via textContent).
 * @param {Array<{label: string, kind: string, value: any, autofocus?: boolean}>} buttons
 * @returns {Promise<any>} Resolves with the clicked button's `value`, or `null`
 *   when dismissed via Escape / backdrop click. Rejects when the document is
 *   unavailable so callers can fall back to the native dialog.
 */
function openDialog({ message, buttons }) {
  if (!canRender()) {
    throw new Error("dialog unavailable: document.body not ready");
  }

  const overlay = document.createElement("div");
  overlay.className =
    "fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");

  const panel = document.createElement("div");
  panel.className =
    "w-full max-w-md rounded-lg shadow-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 overflow-hidden";

  const messageEl = document.createElement("p");
  messageEl.className =
    "px-6 pt-6 pb-2 text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap break-words";
  messageEl.textContent = message;

  const actions = document.createElement("div");
  actions.className = "px-6 py-4 flex justify-end gap-3";

  panel.appendChild(messageEl);
  panel.appendChild(actions);
  overlay.appendChild(panel);

  return new Promise((resolve) => {
    let settled = false;

    const cleanup = () => {
      document.removeEventListener("keydown", onKeydown);
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    };

    const settle = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };

    const onKeydown = (event) => {
      if (event.key === "Escape") settle(null);
    };

    let focusTarget = null;
    for (const button of buttons) {
      const el = document.createElement("button");
      el.type = "button";
      el.className = `${DIALOG_BUTTON_BASE} ${
        DIALOG_BUTTON_STYLES[button.kind] || DIALOG_BUTTON_STYLES.secondary
      }`;
      el.textContent = button.label;
      el.addEventListener("click", () => settle(button.value));
      actions.appendChild(el);
      if (button.autofocus) focusTarget = el;
    }

    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) settle(null);
    });

    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKeydown);
    if (focusTarget) focusTarget.focus();
  });
}

/**
 * Show a two-button confirmation dialog.
 *
 * @param {string} message - The confirmation message.
 * @param {{danger?: boolean, confirmLabel?: string, cancelLabel?: string}} [options]
 * @returns {Promise<boolean>} true when confirmed, false when cancelled.
 */
export function showConfirm(message, options = {}) {
  const danger = options.danger === true;
  return enqueue(() =>
    openDialog({
      message,
      buttons: [
        {
          label: options.confirmLabel || "Confirm",
          kind: danger ? "danger" : "primary",
          value: true,
          autofocus: true,
        },
        { label: options.cancelLabel || "Cancel", kind: "secondary", value: false },
      ],
    })
  ).then(
    (value) => value === true,
    () => {
      try {
        return window.confirm(message);
      } catch {
        return false;
      }
    }
  );
}

/**
 * Show a single-button alert dialog.
 *
 * @param {string} message - The message to display.
 * @param {{okLabel?: string}} [options]
 * @returns {Promise<void>} Resolves when the user dismisses the dialog.
 */
export function showAlert(message, options = {}) {
  return enqueue(() =>
    openDialog({
      message,
      buttons: [
        { label: options.okLabel || "OK", kind: "primary", value: undefined, autofocus: true },
      ],
    })
  ).then(
    () => undefined,
    () => {
      try {
        window.alert(message);
      } catch {
        /* ignore */
      }
    }
  );
}
