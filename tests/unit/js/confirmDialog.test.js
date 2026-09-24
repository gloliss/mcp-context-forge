/**
 * Unit tests for confirm.js — in-app confirm/alert dialog replacements.
 *
 * Covers the contract the call sites rely on:
 *   * showConfirm resolves a Promise<boolean> (true on confirm, false on
 *     cancel / Escape / backdrop click);
 *   * showAlert resolves a Promise<void>;
 *   * dialogs are built with createElement + textContent (never innerHTML), so
 *     a message is never parsed as HTML;
 *   * danger styling is applied to the confirm button;
 *   * at most one dialog is open at a time (concurrent calls are queued);
 *   * when the document is unavailable, showConfirm/showAlert fall back to the
 *     native window.confirm/window.alert so call sites need no fallback branch.
 */

import { describe, test, expect, vi, afterEach } from "vitest";

import { showConfirm, showAlert } from "../../../mcpgateway/admin_ui/confirm.js";

/** Flush pending microtasks/macrotasks so enqueue() chains settle. */
function flush() {
  return new Promise((resolve) => setTimeout(resolve, 10));
}

function dialog() {
  return document.querySelector('[role="dialog"]');
}

function buttonByLabel(label) {
  const overlay = dialog();
  if (!overlay) return null;
  return [...overlay.querySelectorAll("button")].find((b) => b.textContent === label);
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("showConfirm", () => {
  test("returns a Promise<boolean>", async () => {
    const result = showConfirm("confirm?");
    expect(typeof result.then).toBe("function");
    // Settle the dialog so the module-level queue drains for subsequent tests.
    await flush();
    buttonByLabel("Confirm").click();
    await result;
  });

  test("resolves true when the confirm button is clicked", async () => {
    const result = showConfirm("Delete this thing?");
    await flush();

    expect(buttonByLabel("Confirm")).not.toBeNull();
    expect(buttonByLabel("Cancel")).not.toBeNull();
    buttonByLabel("Confirm").click();

    await expect(result).resolves.toBe(true);
  });

  test("resolves false when the cancel button is clicked", async () => {
    const result = showConfirm("Delete this thing?");
    await flush();

    buttonByLabel("Cancel").click();
    await expect(result).resolves.toBe(false);
  });

  test("resolves false when Escape is pressed", async () => {
    const result = showConfirm("Delete this thing?");
    await flush();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await expect(result).resolves.toBe(false);
  });

  test("resolves false when the backdrop is clicked", async () => {
    const result = showConfirm("Delete this thing?");
    await flush();

    dialog().click();
    await expect(result).resolves.toBe(false);
  });

  test("removes the dialog from the DOM after settling", async () => {
    const result = showConfirm("Delete this thing?");
    await flush();
    expect(dialog()).not.toBeNull();

    buttonByLabel("Confirm").click();
    await result;

    expect(dialog()).toBeNull();
  });

  test("applies danger styling to the confirm button when danger is set", async () => {
    const result = showConfirm("Delete this thing?", { danger: true });
    await flush();

    const confirmBtn = buttonByLabel("Confirm");
    expect(confirmBtn.className).toContain("bg-red-600");

    confirmBtn.click();
    await result;
  });

  test("renders the message as text, never as HTML", async () => {
    const message = '<img src=x onerror=alert(1)> <script>bad()</script>';
    const result = showConfirm(message);
    await flush();

    const overlay = dialog();
    expect(overlay.querySelector("img")).toBeNull();
    expect(overlay.querySelector("script")).toBeNull();
    expect(overlay.textContent).toContain(message);

    buttonByLabel("Confirm").click();
    await result;
  });

  test("does not attach inline handlers (no onclick attribute)", async () => {
    const result = showConfirm("Delete this thing?");
    await flush();

    for (const b of dialog().querySelectorAll("button")) {
      expect(b.onclick).toBeNull();
      expect(b.hasAttribute("onclick")).toBe(false);
    }

    buttonByLabel("Confirm").click();
    await result;
  });

  test("queues concurrent calls so only one dialog is open at a time", async () => {
    const first = showConfirm("First question?");
    const second = showConfirm("Second question?");
    await flush();

    expect(document.querySelectorAll('[role="dialog"]').length).toBe(1);
    expect(dialog().textContent).toContain("First question?");

    // Resolve the first; the second should then render.
    buttonByLabel("Confirm").click();
    await first;
    await flush();

    expect(document.querySelectorAll('[role="dialog"]').length).toBe(1);
    expect(dialog().textContent).toContain("Second question?");

    buttonByLabel("Cancel").click();
    await second;
  });

  test("falls back to native window.confirm when document.body is unavailable", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const ownBody = Object.getOwnPropertyDescriptor(document, "body");
    try {
      Object.defineProperty(document, "body", {
        configurable: true,
        get: () => null,
      });

      await expect(showConfirm("are you sure?")).resolves.toBe(true);
      expect(confirmSpy).toHaveBeenCalledWith("are you sure?");
    } finally {
      if (ownBody) {
        Object.defineProperty(document, "body", ownBody);
      } else {
        delete document.body;
      }
    }
  });
});

describe("showAlert", () => {
  test("resolves after the OK button is clicked", async () => {
    const result = showAlert("Something happened");
    await flush();

    expect(buttonByLabel("OK")).not.toBeNull();
    buttonByLabel("OK").click();

    await expect(result).resolves.toBeUndefined();
  });

  test("falls back to native window.alert when document.body is unavailable", async () => {
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    const ownBody = Object.getOwnPropertyDescriptor(document, "body");
    try {
      Object.defineProperty(document, "body", {
        configurable: true,
        get: () => null,
      });

      await expect(showAlert("something")).resolves.toBeUndefined();
      expect(alertSpy).toHaveBeenCalledWith("something");
    } finally {
      if (ownBody) {
        Object.defineProperty(document, "body", ownBody);
      } else {
        delete document.body;
      }
    }
  });
});
