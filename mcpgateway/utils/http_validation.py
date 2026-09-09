# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/http_validation.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP Registry validation errors (PR3).

Mirrors ``mcpgateway/utils/grpc_validation.py`` for the HTTP registry: a
single neutral error type that the HTTP service layer wraps around every
user-facing failure, keeping this module dependency-free so services and
routers can import it without cycles.  Shared HTTP validators (base URL /
target SSRF checks) join this module alongside the HTTP service layer.
"""


class HttpServiceError(Exception):
    """Base class for HTTP registry service-related errors."""
