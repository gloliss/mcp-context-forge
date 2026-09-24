# -*- coding: utf-8 -*-
"""Location: ./tests/playwright/test_admin_i18n_zh.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Chinese (zh-CN) i18n overlay verification.

Run with ``UI_TEST_LOCALE=zh`` so the ``context`` fixture leaves localStorage
unset and the standalone i18n overlay applies the default Chinese translation
(see ``mcpgateway/admin_ui/i18n/standalone.js``).

These tests assert the things the overlay can get wrong in a real browser:
the page must not stay hidden behind the first-paint guard, the ``<html>``
``lang`` attribute must flip to ``zh-CN``, MCP domain nouns must stay English
while UI chrome is translated, and dynamically-injected content (htmx partial
swaps, in-app dialogs, toasts) must be translated by the MutationObserver.
"""

# Third-Party
from playwright.sync_api import expect
import pytest

# Local
from .pages.admin_page import AdminPage


@pytest.mark.ui
class TestAdminI18nZh:
    """Chinese i18n overlay verification (UI_TEST_LOCALE=zh)."""

    def test_lang_attribute_and_ready_flag(self, admin_page: AdminPage):
        """The i18n overlay must set lang=zh-CN and clear the first-paint guard."""
        page = admin_page.page

        # If the overlay threw, standalone.js still sets data-i18n-ready but
        # leaves lang untouched — so assert lang explicitly, not just readiness.
        expect(page.locator("html")).to_have_attribute("data-i18n-ready", "")
        expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
        # The first-paint guard must have revealed the page body.
        expect(page.locator("body")).to_be_visible()

    def test_sidebar_chinese_with_english_mcp_terms(self, admin_page: AdminPage):
        """Sidebar chrome is Chinese; MCP domain nouns stay English."""
        page = admin_page.page

        # At least one of the translatable sidebar labels is present in Chinese.
        # (The sidebar <nav> carries the ``sidebar-scroll`` class; other <nav>
        #  elements are the per-panel tab rows.)
        expect(page.locator("nav.sidebar-scroll")).to_contain_text("概览")
        # MCP domain terms are deliberately left in English.
        expect(page.locator('[data-testid="tools-tab"]')).to_contain_text("Tools")
        expect(page.locator('[data-testid="tools-tab"]')).not_to_contain_text("工具")

    def test_tab_switch_reveals_chinese_header(self, admin_page: AdminPage):
        """Switching tabs reveals a panel whose header is translated to Chinese."""
        page = admin_page.page

        admin_page.click_metrics_tab()
        expect(page.locator("#metrics-panel")).to_be_visible()
        # "System Metrics" heading is a dictionary key (common.js).
        expect(page.locator("#metrics-panel")).to_contain_text("系统指标")

    def test_mutation_observer_translates_injected_content(self, admin_page: AdminPage):
        """Newly injected DOM content (as in an htmx partial swap) is translated."""
        page = admin_page.page

        translated = page.evaluate(
            """async () => {
                const el = document.createElement('div');
                el.id = 'i18n-observer-probe';
                el.textContent = 'System Metrics';
                document.body.appendChild(el);
                // Give the MutationObserver + requestAnimationFrame a chance to flush.
                await new Promise((resolve) => setTimeout(resolve, 700));
                return document.getElementById('i18n-observer-probe').textContent;
            }"""
        )
        assert translated == "系统指标"

    def test_in_app_confirm_dialog_is_chinese(self, admin_page: AdminPage):
        """A dangerous action raises an in-app (non-native) Chinese confirm dialog."""
        page = admin_page.page

        page.evaluate(
            """() => {
                window.__cfConfirmResult = window.Admin.showConfirm(
                    'Are you sure you want to delete the model "gpt-4o"?',
                    { danger: true }
                );
            }"""
        )

        # confirm.js renders the dialog as the only *visible* dialog; the static
        # modal shells (llm-provider/llm-model/tool-package-import) carry the
        # ``hidden`` class, so ``:visible`` disambiguates them.
        dialog = page.locator('div[role="dialog"][aria-modal="true"]:visible')
        expect(dialog).to_be_visible()
        # Interpolated message matched by a translator RULE, domain noun kept as 模型.
        expect(dialog).to_contain_text("确定要删除模型「gpt-4o」吗？")
        # Buttons are dictionary keys ("Confirm" / "Cancel").
        expect(dialog).to_contain_text("取消")

        dialog.get_by_role("button", name="取消").click()

        result = page.evaluate("async () => await window.__cfConfirmResult")
        assert result is False
        expect(
            page.locator('div[role="dialog"][aria-modal="true"]:visible')
        ).to_have_count(0)

    def test_toast_is_chinese(self, admin_page: AdminPage):
        """Toast notifications render their message in Chinese."""
        page = admin_page.page

        page.evaluate(
            """() => window.Admin.showNotification('Member added successfully!', 'success')"""
        )
        expect(page.get_by_text("成员添加成功！")).to_be_visible()
