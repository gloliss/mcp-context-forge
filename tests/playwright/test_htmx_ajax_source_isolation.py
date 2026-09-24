# -*- coding: utf-8 -*-
"""Location: ./tests/playwright/test_htmx_ajax_source_isolation.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Two panels refreshing at once must not share a single htmx request slot.

``htmx.ajax(verb, path, context)`` attributes the request to ``context.source``
and falls back to ``document.body`` when it is absent. htmx keeps ``xhr`` and
``queuedRequests`` on that element, so every source-less call site shares one
slot: a refresh issued while another is in flight is queued, and under the default
``last`` sync strategy already-queued entries are dropped. The refresh then never
happens, and nothing reports it.

Holding one panel's list request open makes that overlap deterministic, so the
test asserts the second panel's request is really issued while the first is still
pending, instead of waiting on a timing guess.

``htmx.ajax`` ignores its ``indicator`` option (an indicator is only resolved from
an ``hx-indicator`` attribute on the request element), so waiting on
``htmx-request`` would prove nothing here.
"""

# Third-Party
import pytest

# Local
from .pages.admin_page import AdminPage


@pytest.mark.ui
class TestPanelRefreshSourceIsolation:
    """A panel refresh must not be queued behind another panel's request."""

    _SOURCES_TAB = "tab-database-sources"
    _SOURCES_PANEL = "database-sources-panel"
    _TEAMS_TAB = "tab-teams"
    _TEAMS_PANEL = "teams-panel"

    def test_teams_refresh_goes_out_while_another_panels_request_is_in_flight(
        self, admin_page: AdminPage
    ):
        """Opening Teams must refresh it even with another panel's request pending."""
        page = admin_page.page
        held = []

        def _hold(route):
            # Never continue: keep this request provably in flight.
            held.append(route)

        page.route("**/admin/database-sources/partial*", _hold)
        try:
            admin_page.click_tab_by_id(self._SOURCES_TAB, self._SOURCES_PANEL)
            for _ in range(200):
                if held:
                    break
                page.wait_for_timeout(50)
            assert held, "the database-sources list request was never intercepted"

            # The database-sources request is still open. The Teams refresh is an
            # independent htmx.ajax call: given its own source it goes out at once;
            # without one it queues on document.body behind the held request and is
            # never issued, so this context manager times out.
            with page.expect_request(
                lambda candidate: "/admin/teams/partial" in candidate.url,
                timeout=20000,
            ) as request_info:
                admin_page.click_tab_by_id(self._TEAMS_TAB, self._TEAMS_PANEL)

            assert "/admin/teams/partial" in request_info.value.url
        finally:
            for route in held:
                route.continue_()
