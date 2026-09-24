# -*- coding: utf-8 -*-
"""Location: ./tests/playwright/test_database_source_modal.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database Source modal scrollability.

The "Add Database Source" form is much taller than the viewport. The modal root
(``#database-source-modal``) must therefore be the scroll container, exactly as
every other modal in ``admin.html`` is (``tool-modal``, ``prompt-modal``,
``gateway-modal``, ``server-modal``, ``llm-provider-modal``, ...). Without
``overflow-y-auto`` on that root the form is clipped inside a ``fixed inset-0``
box and the submit buttons are unreachable.

The form body is fetched by an htmx GET each time the modal opens, so both the
scroll behaviour and the reopen path are covered.
"""

# Third-Party
from playwright.sync_api import expect
import pytest

# Local
from .pages.admin_page import AdminPage


@pytest.mark.ui
class TestDatabaseSourceModal:
    """The Add Database Source modal must scroll its tall form."""

    _MODAL = "#database-source-modal"
    _FORM = "#database-source-create-form"

    def _open_add_form(self, admin_page: AdminPage) -> None:
        """Open the database-sources tab and the Add Database Source form."""
        page = admin_page.page
        admin_page.click_tab_by_id("tab-database-sources", "database-sources-panel")
        page.locator("#add-database-source-btn").click()
        expect(page.locator(self._MODAL)).to_be_visible()
        # The form body arrives via an htmx GET into #database-source-modal-content.
        try:
            expect(page.locator(self._FORM)).to_be_visible(timeout=30000)
        except AssertionError as exc:
            modal_html = page.eval_on_selector(
                "#database-source-modal-content", "el => el.innerHTML.slice(0, 2000)"
            )
            raise AssertionError(
                f"Add Database Source form did not load; modal content was: {modal_html!r}"
            ) from exc

    def test_add_form_scrolls_to_reach_submit_buttons(self, admin_page: AdminPage):
        """The tall form scrolls inside the modal so the submit buttons stay reachable."""
        page = admin_page.page
        self._open_add_form(admin_page)

        # The modal root — not the page — is the scroll container.
        overflow_y = page.eval_on_selector(
            self._MODAL, "el => getComputedStyle(el).overflowY"
        )
        assert overflow_y == "auto", (
            "#database-source-modal must be able to scroll its tall form; "
            f"got overflow-y={overflow_y!r}"
        )

        metrics = page.evaluate(
            f"""() => {{
                const el = document.querySelector('{self._MODAL}');
                return {{scrollHeight: el.scrollHeight, clientHeight: el.clientHeight}};
            }}"""
        )
        assert metrics["scrollHeight"] > metrics["clientHeight"], (
            "the Add Database Source form should be taller than the viewport; "
            f"got {metrics}"
        )

        # The bottom action row must be reachable by scrolling to it.
        submit = page.locator(f'{self._MODAL} button[type="submit"]')
        submit.scroll_into_view_if_needed()
        expect(submit).to_be_in_viewport()
        assert page.eval_on_selector(self._MODAL, "el => el.scrollTop") > 0, (
            "the modal did not scroll after revealing the submit button"
        )

    def test_add_form_reloads_after_closing(self, admin_page: AdminPage):
        """Reopening the modal after Cancel loads the form again (not stuck on Loading)."""
        page = admin_page.page
        self._open_add_form(admin_page)

        page.locator(f'{self._MODAL} button:has-text("Cancel")').click()
        expect(page.locator(self._MODAL)).to_be_hidden()

        self._open_add_form(admin_page)
