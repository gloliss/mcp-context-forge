# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/adapters/database/tls.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

TLS parameter mapping for the database adapters (OB-07).

A database source asks for TLS with one vocabulary per engine
(``disabled``/``preferred``/``required``/``verify_ca``/``verify_full``), while
each driver spells the same intent differently.  The translations live here so
the native adapters and the OceanBase providers cannot drift apart.
"""

# Standard
from pathlib import Path
import ssl
from typing import Any, Optional

# First-Party
from mcpgateway.adapters.database.exceptions import DatabaseAdapterError

#: TLS modes a database source may request; mirrors the API schema's Literal.
SSL_MODES = ("disabled", "preferred", "required", "verify_ca", "verify_full")


def normalized_ssl_mode(source: Any) -> Optional[str]:
    """Return the source's ``ssl_mode`` in canonical form, or ``None``.

    Args:
        source: A ``DatabaseSource`` (or compatible object).

    Returns:
        Optional[str]: One of :data:`SSL_MODES`, or ``None`` when unset.

    Raises:
        DatabaseAdapterError: If the configured mode is unrecognized; an
            unknown TLS mode must never mean "no TLS settings".
    """
    mode = getattr(source, "ssl_mode", None)
    if mode is None or str(mode).strip() == "":
        return None
    normalized = str(mode).strip().lower()
    if normalized not in SSL_MODES:
        raise DatabaseAdapterError(f"Unsupported ssl_mode {mode!r}; expected one of {', '.join(SSL_MODES)}")
    return normalized


def system_ca_bundle() -> str:
    """Return the system CA bundle path backing the certificate-verifying modes.

    Returns:
        str: Path to a readable PEM bundle.

    Raises:
        DatabaseAdapterError: If no bundle is discoverable, because a
            ``verify_*`` mode that cannot verify must fail rather than connect.
    """
    cafile = ssl.get_default_verify_paths().cafile
    if not cafile or not Path(cafile).is_file():
        raise DatabaseAdapterError("Certificate verification is unavailable: no system CA bundle was found")
    return cafile


def pymysql_ssl_args(mode: Optional[str]) -> dict[str, Any]:
    """Translate ``mode`` into PyMySQL's TLS parameters.

    PyMySQL's own default is "preferred" — it attempts TLS and falls back —
    which is what an unset mode or ``preferred`` means here.  ``required``
    forces TLS without certificate verification.  The ``verify_*`` modes pass an
    explicit CA bundle because PyMySQL only enables hostname verification when
    one is supplied: without it ``verify_full`` would be silently downgraded to
    ``verify_ca``.
    """
    if mode is None or mode == "preferred":
        return {}
    if mode == "disabled":
        return {"ssl_disabled": True}
    if mode == "required":
        return {"ssl_verify_cert": False}
    args: dict[str, Any] = {"ssl_verify_cert": True, "ssl_ca": system_ca_bundle()}
    if mode == "verify_full":
        args["ssl_verify_identity"] = True
    return args


#: Source ``ssl_mode`` mapped onto libpq's ``sslmode`` vocabulary.
LIBPQ_SSLMODES = {
    "disabled": "disable",
    "preferred": "prefer",
    "required": "require",
    "verify_ca": "verify-ca",
    "verify_full": "verify-full",
}


def libpq_ssl_args(mode: Optional[str]) -> dict[str, Any]:
    """Translate ``mode`` into libpq's ``sslmode``.

    libpq has a mode for every :data:`SSL_MODES` value, so the configured value
    is honored exactly.  The verifying modes point ``sslrootcert`` at the system
    trust store instead of libpq's per-user default
    (``~/.postgresql/root.crt``), which usually does not exist.
    """
    if mode is None:
        return {}
    args: dict[str, Any] = {"sslmode": LIBPQ_SSLMODES[mode]}
    if mode in ("verify_ca", "verify_full"):
        args["sslrootcert"] = system_ca_bundle()
    return args


def oracledb_protocol_args(mode: Optional[str]) -> dict[str, Any]:
    """Translate ``mode`` into the Oracle wire protocol.

    The Oracle driver selects TLS through the protocol rather than a mode flag
    and has no opportunistic variant, so ``preferred`` and ``required`` both
    mean TCPS.  The verifying modes are refused instead of silently downgraded:
    certificate verification needs a wallet or CA the source model has no field
    to carry.

    Raises:
        DatabaseAdapterError: For the verifying modes, which this model cannot
            express.
    """
    if mode is None:
        return {}
    if mode in ("verify_ca", "verify_full"):
        raise DatabaseAdapterError(f"ssl_mode {mode!r} needs an Oracle wallet, which a database source cannot configure")
    return {"protocol": "tcp" if mode == "disabled" else "tcps"}
