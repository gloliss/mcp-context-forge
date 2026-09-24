# -*- coding: utf-8 -*-
"""Location: ./tests/playwright/test_database_source_modal.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Database Source modal: scrollability, and isolation from other htmx requests.

Two defects are covered here.

1. **Scrollability.** The "Add Database Source" form is much taller than the
   viewport, so the modal root (``#database-source-modal``) must be the scroll
   container -- exactly as the other admin modals are (``tool-modal``,
   ``prompt-modal``, ``gateway-modal``, ``server-modal``, ...). Without
   ``overflow-y-auto`` on a ``fixed inset-0`` root the form is clipped and the
   submit buttons are unreachable.

2. **Request isolation.** ``htmx.ajax(verb, path, context)`` without ``source``
   resolves its request element to ``document.body``, and htmx keys in-flight
   state (``xhr``, ``queuedRequests``) on that element. Every source-less
   ``htmx.ajax`` call therefore shares one slot: while one is in flight a later
   modal load is queued, and under the default ``last`` sync strategy the
   already-queued entries are dropped. The modal then sits on its "Loading…"
   placeholder forever. Passing the modal content element as ``source`` gives
   each modal load its own slot.

   Note that ``htmx.ajax`` ignores the ``indicator`` option entirely (it uses
   ``addRequestIndicatorClasses``, which looks for an ``hx-indicator``
   attribute), so waiting on ``htmx-request`` for ``#database-sources-loading``
   would be a no-op -- the race below is reproduced by holding the request
   instead.
"""

# Third-Party
from playwright.sync_api import expect
import pytest

# Local
from .pages.admin_page import AdminPage

# Modal roots that wrap a ``min-h-screen`` inner container. Each one must scroll
# on its own root; kept as an explicit list so a newly added sibling modal
# cannot silently regress.
MODAL_ROOTS_WITH_MIN_SCREEN_INNER = [
    "bulk-import-modal",
    "user-edit-modal",
    "create-team-modal",
    "team-edit-modal",
    "team-join-requests-modal",
    "invite-user-modal",
    "role-assignment-modal",
    "database-source-modal",
]


@pytest.mark.ui
class TestDatabaseSourceModal:
    """The Add Database Source modal must scroll its tall form."""

    _MODAL = "#database-source-modal"
    _FORM = "#database-source-create-form"
    _TAB = "tab-database-sources"
    _PANEL = "database-sources-panel"

    def _open_add_form(self, admin_page: AdminPage) -> None:
        """Open the database-sources tab and the Add Database Source form."""
        page = admin_page.page
        admin_page.click_tab_by_id(self._TAB, self._PANEL)
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

        # The modal root -- not the page -- is the scroll container.
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

    def test_add_form_loads_while_the_panel_list_request_is_in_flight(
        self, admin_page: AdminPage
    ):
        """Opening the modal must not be queued behind the panel's own list request.

        Regression test for the "Loading…" hang: the panel issues its list request
        when the tab is opened, and clicking "Add Database Source" immediately
        after issues a second, independent request. Holding the first one open
        makes that overlap deterministic instead of a timing guess.
        """
        page = admin_page.page
        held = []

        def _hold(route):
            # Never continue: keep the panel's list request provably in flight.
            held.append(route)

        page.route("**/admin/database-sources/partial*", _hold)
        try:
            admin_page.click_tab_by_id(self._TAB, self._PANEL)
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(50)
            assert held, "the database-sources panel list request was never intercepted"

            page.locator("#add-database-source-btn").click()
            expect(page.locator(self._MODAL)).to_be_visible()
            # Without its own request element this stays on the "Loading…"
            # placeholder, because htmx queues it on document.body behind the
            # held request.
            expect(page.locator(self._FORM)).to_be_visible(timeout=30000)
        finally:
            for route in held:
                route.continue_()


@pytest.mark.ui
class TestModalsUseTheScrollPattern:
    """Each modal with a ``min-h-screen`` inner container scrolls on its root."""

    def test_modal_roots_can_scroll(self, admin_page: AdminPage):
        """Every modal root resolves to ``overflow-y: auto``.

        The inner ``min-h-screen`` container is taller than a short viewport, so
        a non-scrolling root clips the modal's action buttons. ``getComputedStyle``
        resolves the Tailwind utility regardless of the root being hidden, so the
        modals do not need to be opened here.
        """
        results = admin_page.page.evaluate(
            """(ids) => Object.fromEntries(ids.map((id) => {
                const el = document.getElementById(id);
                return [id, el ? getComputedStyle(el).overflowY : 'MISSING'];
            }))""",
            MODAL_ROOTS_WITH_MIN_SCREEN_INNER,
        )
        not_scrollable = {mid: oy for mid, oy in results.items() if oy != "auto"}
        assert not not_scrollable, (
            "modal roots must scroll their tall content; these do not: "
            f"{not_scrollable}"
        )
