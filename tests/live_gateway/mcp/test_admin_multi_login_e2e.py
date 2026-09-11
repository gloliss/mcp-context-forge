# -*- coding: utf-8 -*-
"""Location: ./tests/live_gateway/mcp/test_admin_multi_login_e2e.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Live-gateway black-box E2E for the Admin UI multi-session guarantee (issue #2).

Two independent HTTP sessions ("locations") log in with the same admin account.
The test asserts the requirement "一处登陆不会将另外一处的 admin 账号顶掉":

* AC-1 — after a second login, the first session's ``jwt_token`` still authorises
  ``/admin`` (no kick-out).
* AC-2 — logging out the first session revokes only it; the second session still
  authorises ``/admin`` (logout isolation).

Requirements:
    - ContextForge running and reachable at ``BASE_URL`` (default
      ``http://127.0.0.1:8080``); the suite skips otherwise.
    - A working admin credential (``PLATFORM_ADMIN_EMAIL`` /
      ``PLATFORM_ADMIN_PASSWORD``). The suite skips when no candidate password
      logs in, or when the account is still on the forced password-change flow —
      it never rotates the shared admin password.

Usage:
    pytest tests/live_gateway/mcp/test_admin_multi_login_e2e.py -v -s
"""

# Future
from __future__ import annotations

# Standard
from collections.abc import Generator
import os
import socket

# Third-Party
import httpx
import pytest

# Local
from ..helpers.mcp_test_helpers import ADMIN_EMAIL, BASE_URL, skip_no_gateway

pytestmark = [pytest.mark.e2e, skip_no_gateway]

# The session-wide autouse ``_deterministic_dns`` fixture in tests/conftest.py
# replaces ``socket.getaddrinfo`` with a stub that maps every non-loopback host
# to a fixed public IP (so SSRF validators run without real DNS). That breaks
# live-gateway tests targeting a LAN address like 10.10.100.15. This module
# installs the real resolver for the duration of each test and reinstates
# whatever was in place on teardown, so the session-wide stub is restored and
# later tests keep their deterministic DNS.
_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def _real_dns_for_live_gateway() -> Generator[None, None, None]:
    """Use the real resolver for this module's tests only, then restore."""
    previous = socket.getaddrinfo
    socket.getaddrinfo = _REAL_GETADDRINFO
    yield
    socket.getaddrinfo = previous


# Mirrors tests/playwright/conftest.py candidate ordering; the post-rotation
# password is tried first because a long-lived instance has usually rotated.
# Both default to the shared env vars, so a deployment-specific credential
# never has to be committed here.
_ADMIN_PASSWORD_CANDIDATES = [
    os.getenv("PLATFORM_ADMIN_NEW_PASSWORD", "SV^cB9Qx3!em48fy$1VhjxkW"),  # pragma: allowlist secret
    os.getenv("PLATFORM_ADMIN_PASSWORD", "5S1Nd8z$Ivb6N%Lsj^okvVF6"),  # pragma: allowlist secret
]

_LOGIN_PATH = "/admin/login"
# The authenticated dashboard. The trailing slash matters: Starlette issue a 307
# slash-redirect for ``/admin`` → ``/admin/``, so probing ``/admin`` would report
# 307 even for a healthy session.
_ADMIN_PATH = "/admin/"
_ADMIN_COOKIE = "jwt_token"
_CSRF_COOKIE = "mcpgateway_csrf_token"
_CSRF_FIELD = "csrf_token"
_CSRF_HEADER = "x-csrf-token"


def _browser_headers() -> dict[str, str]:
    """Headers that make the request look like a same-origin browser navigation."""
    return {"Accept": "text/html", "Origin": BASE_URL, "Referer": f"{BASE_URL}{_LOGIN_PATH}"}


def _login(client: httpx.Client, username: str, password: str) -> str:
    """Perform the Admin UI login form flow on ``client``.

    Returns a short status string: ``ok``, ``error``, ``change_password_required``,
    ``no_form`` or ``network_error``. The GET first mints the pre-auth CSRF nonce
    that POST /admin/login validates against when CSRF is enabled.
    """
    try:
        page = client.get(f"{BASE_URL}{_LOGIN_PATH}", headers={"Accept": "text/html"}, follow_redirects=False)
    except httpx.HTTPError:
        return "network_error"

    # No login form (email auth disabled / already authenticated): cannot exercise.
    if page.status_code != 200 or _LOGIN_PATH not in str(getattr(page, "url", "")):
        return "no_form"

    csrf = client.cookies.get(_CSRF_COOKIE) or ""
    try:
        resp = client.post(
            f"{BASE_URL}{_LOGIN_PATH}",
            data={"email": username, "password": password, _CSRF_FIELD: csrf},
            headers={**_browser_headers(), _CSRF_HEADER: csrf},
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return "network_error"

    location = resp.headers.get("location", "") or ""
    if "change-password-required" in location:
        return "change_password_required"
    if "error=" in location:
        return "error"
    # Success redirects to the dashboard and sets the session cookie.
    if location.rstrip("/").endswith("/admin") and client.cookies.get(_ADMIN_COOKIE):
        return "ok"
    return "error"


def _admin_response(client: httpx.Client) -> httpx.Response:
    """Return the (non-redirected) response for the authenticated dashboard."""
    return client.get(f"{BASE_URL}{_ADMIN_PATH}", headers={"Accept": "text/html"}, follow_redirects=False)


def _logout(client: httpx.Client) -> None:
    """POST /admin/logout for ``client`` (CSRF token attached when present)."""
    csrf = client.cookies.get(_CSRF_COOKIE) or ""
    client.post(
        f"{BASE_URL}/admin/logout",
        data={_CSRF_FIELD: csrf},
        headers={**_browser_headers(), _CSRF_HEADER: csrf},
        follow_redirects=False,
    )


def _establish_admin_session(client: httpx.Client) -> str:
    """Log ``client`` in, trying candidate admin passwords.

    Returns the status of the last attempt (``ok`` on success).
    """
    status = "no_form"
    for password in _ADMIN_PASSWORD_CANDIDATES:
        status = _login(client, ADMIN_EMAIL, password)
        if status == "ok":
            return "ok"
        if status in ("network_error", "no_form"):
            break
    return status


@pytest.fixture
def admin_sessions() -> Generator[tuple[httpx.Client, httpx.Client], None, None]:
    """Two independent logged-in Admin UI sessions ("locations"), or skip.

    Skips when the gateway offers no usable admin login (no form / no candidate
    password works / the account is mid forced-password-change) so the suite
    stays green on deployments this E2E cannot exercise.
    """
    with httpx.Client(timeout=15.0) as session_a, httpx.Client(timeout=15.0) as session_b:
        status_a = _establish_admin_session(session_a)
        if status_a != "ok":
            pytest.skip(f"admin login unavailable for the multi-login E2E ({status_a})")
        assert _establish_admin_session(session_b) == "ok", "second admin login failed in the same gateway"
        yield session_a, session_b


class TestAdminMultiLoginE2E:
    """Two live admin sessions must not invalidate each other."""

    def test_second_login_does_not_kick_out_first(self, admin_sessions: tuple[httpx.Client, httpx.Client]) -> None:
        """AC-1: a login at location B leaves location A's session valid."""
        session_a, session_b = admin_sessions
        assert _admin_response(session_a).status_code == 200

        # Each location holds its own, distinct session token.
        token_a = session_a.cookies.get(_ADMIN_COOKIE)
        token_b = session_b.cookies.get(_ADMIN_COOKIE)
        assert token_a and token_b and token_a != token_b

        # AC-1: the second login did NOT kick out the first session.
        assert _admin_response(session_a).status_code == 200, "a second login invalidated the first admin session"

    def test_logout_is_isolated_to_the_calling_session(self, admin_sessions: tuple[httpx.Client, httpx.Client]) -> None:
        """AC-2: logging out location A leaves location B's session valid."""
        session_a, session_b = admin_sessions
        assert _admin_response(session_b).status_code == 200

        _logout(session_a)

        # A is logged out: the session cookie is cleared and the dashboard no
        # longer serves that browser session.
        assert session_a.cookies.get(_ADMIN_COOKIE) is None
        logged_out = _admin_response(session_a)
        assert logged_out.status_code != 200, "logged-out session should not reach the dashboard"
        assert "/admin/login" in (logged_out.headers.get("location") or "")

        # AC-2: B's session survived A's logout.
        assert _admin_response(session_b).status_code == 200, "logging out one session revoked another admin session"
