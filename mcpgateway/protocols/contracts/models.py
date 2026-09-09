# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/models.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Operation IR models (PR1, extended by PR3).

``OperationDefinition`` is the long-lived intermediate representation a
protocol contract (OpenAPI, WSDL, proto, or the legacy REST tool table)
compiles into before invocation.  PR3 adds the catalog/diagnostic/artifact
value objects around it (design-document §5).
"""

# Standard
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class OperationDefinition:
    """Protocol-agnostic description of one invocable operation.

    Attributes:
        key: Stable operation key (e.g. ``GET /v1/lots/{lotId}`` for HTTP,
            ``package.Service.Method`` for gRPC, or the PR1 legacy
            ``http:rest:<tool_id>`` form).
        protocol: Target protocol (``"http"`` or ``"grpc"``).
        source_operation_id: Source-specific operation identifier
            (OpenAPI operationId, legacy tool id, ...).
        title: Short human-readable title.
        description: Optional longer description.
        deprecated: Whether the source contract marks this deprecated.
        tags: Source contract tags.
        request: Protocol-specific request shape (PR1 HTTP: url, method,
            query_mapping, header_mapping; PR3 HTTP: base_url, method,
            path_template).
        response: Protocol-specific response shape (PR1 HTTP:
            output_schema, jsonpath_filter).
        extensions: Source/debug/UI metadata only — runtime-required
            information must live in typed fields, never here.
    """

    key: str
    protocol: Literal["http", "grpc"]
    source_operation_id: str | None = None
    title: str | None = None
    description: str | None = None
    deprecated: bool = False
    tags: tuple[str, ...] = ()
    request: Any = None
    response: Any = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContractDiagnostic:
    """A non-fatal problem found while compiling a contract (design §5.1).

    Attributes:
        severity: ``"warning"`` (recoverable, artifact still usable) or
            ``"error"`` (one operation skipped; the catalog may be partial).
        code: Stable machine-readable code (e.g. ``duplicate-operation-key``,
            ``external-ref-unresolved``).
        message: Human-readable description.
        location: Source location (e.g. ``paths./v1/lots.get``) when known.
        operation_key: Stable operation key when the diagnostic is scoped
            to one operation.
    """

    severity: Literal["warning", "error"]
    code: str
    message: str
    location: str | None = None
    operation_key: str | None = None


class ContractProviderError(Exception):
    """Raised by a :class:`ContractProvider` when whole-document discovery fails.

    Per-operation failures are reported as diagnostics; this error means the
    artifact itself could not be parsed or validated (design §6.2).
    """


@dataclass(frozen=True)
class ContractArtifact:
    """Immutable contract document handed to a :class:`ContractProvider`.

    The payload is the post-materialization document (external ``$ref``
    targets already bundled locally); providers never access the network.

    Attributes:
        payload: Raw document bytes (JSON or YAML; ZIP bundles are unpacked
            by ``ContractArtifactService`` before discovery).
        artifact_format: One of ``openapi-json``/``openapi-yaml``/``wsdl``/
            ``xsd`` (design §11.4).
        source_type: One of ``openapi-url``/``openapi-upload``/``manual``/
            ... (design §11.3).
        source_info: Provenance metadata (filename, origin URL, bundling
            log, diagnostics summary).
    """

    payload: bytes
    artifact_format: str
    source_type: str
    source_info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryContext:
    """Read-only context for a :class:`ContractProvider` discovery run.

    Attributes:
        ref_fetcher: Optional async callback ``(url) -> bytes`` used to
            materialise external ``$ref`` targets that survived bundling.
            ``None`` means remote references are reported as diagnostics
            instead of fetched.
        limits: Numeric limits (max document bytes, max operations, ...)
            supplied by the caller; providers must not read global settings.
    """

    ref_fetcher: Optional[Callable[[str], Awaitable[bytes]]] = None
    limits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationCatalog:
    """The compiled result of one contract discovery run (design §5.1).

    Attributes:
        source_type: ``openapi``/``wsdl``/``grpc-reflection``/``proto``/
            ``protoset``/``manual-http``.
        source_hash: Content hash of the source artifact the catalog was
            compiled from.
        operations: Compiled operations, in deterministic order.
        diagnostics: Non-fatal problems encountered during compilation.
    """

    source_type: str
    source_hash: str
    operations: tuple[OperationDefinition, ...]
    diagnostics: tuple[ContractDiagnostic, ...] = ()
