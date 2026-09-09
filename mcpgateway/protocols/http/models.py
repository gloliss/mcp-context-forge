# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/models.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Typed HTTP contract value objects (PR3, design §9.3–9.5).

These frozen dataclasses are the typed ``request``/``response`` payloads of
an :class:`~mcpgateway.protocols.contracts.models.OperationDefinition` for
registry-compiled HTTP operations.  ``HttpRequestContract`` feeds the
``OperationToolCompiler`` input-schema grouping and the runtime
``RequestBuilder``; ``HttpResponseContract`` feeds output-schema generation
and the ``ResponseDecoder`` media-type preference.
"""

# Standard
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class HttpParameter:
    """One request parameter (design §9.4).

    Attributes:
        name: Parameter name as it appears in the contract.
        location: Where the parameter travels (path/query/header/cookie).
        required: Whether the contract marks it required.
        schema: Resolved JSON Schema fragment (internal ``$ref`` already
            dereferenced).
        style: OpenAPI serialization style (``form``, ``simple``, ...).
        explode: OpenAPI ``explode`` flag (None when unspecified).
        allow_reserved: OpenAPI ``allowReserved`` flag.
    """

    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    schema: dict[str, Any]
    style: str | None = None
    explode: bool | None = None
    allow_reserved: bool = False


@dataclass(frozen=True)
class HttpBodyVariant:
    """One request-body variant for a media type (design §9.5).

    Attributes:
        media_type: The media type (e.g. ``application/json``).
        codec: The codec name resolving the media type
            (``json``/``form``/``multipart``/``text``/``binary``).
        schema: Resolved schema fragment, or None when the body has no
            schema (e.g. raw binary).
        required: Whether the enclosing requestBody marks itself required.
        schema_ref: The original ``$ref`` string (post-materialization),
            kept for provenance only.
    """

    media_type: str
    codec: str
    schema: dict[str, Any] | None
    required: bool = False
    schema_ref: str | None = None


@dataclass(frozen=True)
class HttpRequestContract:
    """Complete request side of an HTTP operation (design §9.3).

    Attributes:
        method: Uppercase HTTP method.
        path_template: Raw path template (e.g. ``/v1/lots/{lotId}``).
        parameters: All parameters across path/query/header/cookie.
        bodies: All body variants, in contract order (empty for bodyless
            methods).
    """

    method: str
    path_template: str
    parameters: tuple[HttpParameter, ...]
    bodies: tuple[HttpBodyVariant, ...]


@dataclass(frozen=True)
class HttpResponseVariant:
    """One response variant for a status-code/media-type pair.

    Attributes:
        status_code: ``"200"``/``"204"``/``"default"``/``"2XX"`` ... as
            declared by the contract.
        media_type: The media type.
        schema: Resolved schema fragment (None for empty bodies).
        description: Contract-provided response description.
    """

    status_code: str
    media_type: str
    schema: dict[str, Any] | None
    description: str | None = None


@dataclass(frozen=True)
class HttpResponseContract:
    """Complete response side of an HTTP operation.

    Attributes:
        variants: All response variants across status codes, in contract
            order (2xx first for determinism, then others).
    """

    variants: tuple[HttpResponseVariant, ...] = ()
