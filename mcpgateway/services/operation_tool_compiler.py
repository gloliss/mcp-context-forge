# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/operation_tool_compiler.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Compile one registry HTTP operation into an MCP tool definition (PR3,
design §17).

The compiler is a **pure function** over its inputs: it never touches the
database, the network, settings, or ``openapi_core``.  ``service`` and
``artifact`` are duck-typed (any object carrying ``slug``/``base_url`` and
``content_hash``/``source_type`` attributes respectively), so this module
does not import SQLAlchemy models.

The generated ``input_schema`` groups parameters by transport location
(``path``/``query``/``headers``/``cookies``/``body``) — only groups that
actually exist are emitted.  ``protocol_config`` follows the strict §18
shape and never embeds JSON Schema.
"""

# Standard
from dataclasses import dataclass
from typing import Any

# First-Party
from mcpgateway.common.validators import SecurityValidator
from mcpgateway.schemas import ToolCreate
from mcpgateway.utils.create_slug import slugify

# Media-type priority for the request body group (design §17): the first
# matching entry wins; anything else ranks last.
_BODY_MEDIA_TYPE_PRIORITY = (
    "application/json",
    "+json",
    "text/",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "application/octet-stream",
)

_NO_BODY_RESPONSE_SCHEMA = {"type": "object", "description": "No response body is returned"}
_EMPTY_OUTPUT_SCHEMA = {"type": "object", "description": "No supported response schema"}

# Parameter location -> input-schema group name.
_GROUP_NAMES = {"path": "path", "query": "query", "header": "headers", "cookie": "cookies"}

# Output-schema selection: exact 200 first, then numeric 2xx ascending,
# then the ``2XX`` range form (design §17).
_RESPONSE_RANGE_STATUSES = ("2XX", "2xx")

_NO_CONTENT_STATUSES = ("204", "205")
_KNOWN_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


@dataclass(frozen=True)
class ToolCompileOverrides:
    """Optional caller-supplied overrides for tool compilation (design §17).

    Attributes:
        name: Replacement tool name; ``None`` derives the default name.
        description: Replacement description; ``None`` derives it from the
            operation.
        input_schema: Complete replacement input schema; ``None`` compiles
            the grouped schema.
        output_schema: Complete replacement output schema; ``None``
            compiles it from the operation responses.
    """

    name: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class OperationToolCompiler:
    """Compile registry HTTP operations into ``ToolCreate`` payloads."""

    def compile(self, operation, service, artifact, overrides: ToolCompileOverrides) -> ToolCreate:
        """Compile one operation into a ``ToolCreate``.

        Args:
            operation: An :class:`~mcpgateway.protocols.contracts.models.OperationDefinition`
                whose ``request`` is an ``HttpRequestContract``.
            service: Duck-typed HTTP service carrying ``slug`` and
                ``base_url`` attributes.
            artifact: Duck-typed schema artifact carrying ``content_hash``
                and ``source_type`` attributes (provenance only).
            overrides: Optional caller overrides applied after compilation.

        Returns:
            A fully populated ``ToolCreate`` ready for ``DbTool``
            construction by the caller.

        Raises:
            ValueError: If ``operation.request`` is not an
                ``HttpRequestContract``.
        """
        request = operation.request
        if not hasattr(request, "parameters") or not hasattr(request, "bodies"):
            raise ValueError(f"Operation {operation.key!r} has no HTTP request contract")

        method = request.method.upper()
        path_template = request.path_template
        response_variants = self._response_variants(operation)
        no_body_response = method == "HEAD" or any(variant.status_code in _NO_CONTENT_STATUSES for variant in response_variants)
        picked_response = None if no_body_response else self._pick_response_variant(response_variants)
        body_variant = self._pick_body_variant(request.bodies)

        input_schema = overrides.input_schema if overrides.input_schema is not None else self._compile_input_schema(request, body_variant)
        output_schema = overrides.output_schema if overrides.output_schema is not None else self._compile_output_schema(picked_response, no_body_response)
        protocol_config = self._compile_protocol_config(operation, method, path_template, body_variant, picked_response)

        name = overrides.name if overrides.name is not None else self._default_tool_name(service.slug, operation)
        description = overrides.description if overrides.description is not None else self._compile_description(operation, method, path_template)

        annotations = {
            "readOnlyHint": method in ("GET", "HEAD"),
            "httpArtifact": {"contentHash": artifact.content_hash, "sourceType": artifact.source_type},
            "httpResponses": self._compile_response_annotations(operation),
        }

        return ToolCreate(
            name=name,
            description=description,
            integration_type="REST",
            request_type=method if method in _KNOWN_METHODS else "POST",
            input_schema=input_schema,
            output_schema=output_schema,
            annotations=annotations,
            base_url=service.base_url,
            protocol_config=protocol_config,
        )

    @staticmethod
    def _compile_input_schema(request, body_variant) -> dict[str, Any]:
        """Build the grouped input schema for one request contract."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for location in ("path", "query", "header", "cookie"):
            params = [param for param in request.parameters if param.location == location]
            if not params:
                continue
            group_name = _GROUP_NAMES[location]
            group = OperationToolCompiler._compile_parameter_group(params, force_all_required=location == "path")
            properties[group_name] = group
            if group["required"]:
                required.append(group_name)
        if body_variant is not None:
            properties["body"] = OperationToolCompiler._compile_body_group(body_variant)
            if body_variant.required:
                required.append("body")
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    @staticmethod
    def _compile_parameter_group(params, force_all_required: bool = False) -> dict[str, Any]:
        """Build one location group schema from its parameters."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in params:
            properties[param.name] = param.schema
            if param.required or force_all_required:
                required.append(param.name)
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    @staticmethod
    def _compile_body_group(variant) -> dict[str, Any]:
        """Build the body group schema for the picked variant."""
        group = dict(variant.schema) if variant.schema is not None else {"type": "string"}
        group.setdefault("description", f"Request body ({variant.media_type})")
        return group

    @staticmethod
    def _pick_body_variant(variants):
        """Pick the preferred body variant by media-type priority."""
        if not variants:
            return None
        return min(variants, key=_body_variant_rank)

    @staticmethod
    def _compile_output_schema(picked_response, no_body_response: bool) -> dict[str, Any]:
        """Compile the output schema from the picked response variant."""
        if no_body_response:
            return dict(_NO_BODY_RESPONSE_SCHEMA)
        if picked_response is None:
            return dict(_EMPTY_OUTPUT_SCHEMA)
        return dict(picked_response.schema) if picked_response.schema is not None else dict(_EMPTY_OUTPUT_SCHEMA)

    @staticmethod
    def _pick_response_variant(variants):
        """Pick the output response variant (200, then 2xx ascending)."""
        supported = [variant for variant in variants if _supports_output_schema(variant)]
        exact_200 = next((variant for variant in supported if variant.status_code == "200"), None)
        if exact_200 is not None:
            return exact_200
        numeric = sorted(
            (variant for variant in supported if variant.status_code.isdigit() and 200 <= int(variant.status_code) < 300),
            key=lambda variant: int(variant.status_code),
        )
        if numeric:
            return numeric[0]
        return next((variant for variant in supported if variant.status_code in _RESPONSE_RANGE_STATUSES), None)

    @staticmethod
    def _compile_protocol_config(operation, method: str, path_template: str, body_variant, picked_response) -> dict[str, Any]:
        """Build the strict §18 protocol_config payload (no JSON Schema inside)."""
        protocol_config: dict[str, Any] = {
            "version": 1,
            "operationRef": operation.key,
            "request": {"method": method, "pathTemplate": path_template},
            "response": {
                "codec": "auto",
                "preferredMediaTypes": [picked_response.media_type] if picked_response is not None else [],
            },
            "streaming": {"mode": "none"},
        }
        if body_variant is not None:
            protocol_config["request"]["preferredContentType"] = body_variant.media_type
            protocol_config["request"]["body"] = {"codec": body_variant.codec, "mediaType": body_variant.media_type}
        return protocol_config

    @staticmethod
    def _compile_response_annotations(operation) -> dict[str, Any]:
        """Compile UI-only response metadata (never part of protocol_config)."""
        annotations: dict[str, Any] = {}
        for variant in OperationToolCompiler._response_variants(operation):
            entry: dict[str, Any] = {"mediaType": variant.media_type}
            if variant.description is not None:
                entry["description"] = variant.description
            if variant.schema is not None:
                entry["schema"] = variant.schema
            annotations[variant.status_code] = entry
        return annotations

    @staticmethod
    def _response_variants(operation) -> tuple:
        """Return the response variants of an operation (empty for none)."""
        if operation.response is None or not hasattr(operation.response, "variants"):
            return ()
        return operation.response.variants

    @staticmethod
    def _default_tool_name(service_slug: str, operation) -> str:
        """Derive the deterministic tool name (design §17 naming)."""
        request = operation.request
        method = request.method.lower()
        source = operation.source_operation_id or method
        path_slug = slugify(request.path_template).strip("-_") or method
        raw_name = f"{service_slug}__{source}__{path_slug}"
        return SecurityValidator.validate_name(raw_name, "Tool name")

    @staticmethod
    def _compile_description(operation, method: str, path_template: str) -> str:
        """Derive the tool description from the operation metadata."""
        return operation.description or operation.title or f"{method} {path_template}"


def _body_variant_rank(variant) -> int:
    """Rank one body variant by the §17 media-type priority."""
    media_type = (variant.media_type or "").lower()
    if media_type == _BODY_MEDIA_TYPE_PRIORITY[0]:
        return 0
    if media_type.endswith(_BODY_MEDIA_TYPE_PRIORITY[1]):
        return 1
    if media_type.startswith(_BODY_MEDIA_TYPE_PRIORITY[2]):
        return 2
    if media_type == _BODY_MEDIA_TYPE_PRIORITY[3]:
        return 3
    if media_type == _BODY_MEDIA_TYPE_PRIORITY[4]:
        return 4
    if media_type == _BODY_MEDIA_TYPE_PRIORITY[5]:
        return 5
    return 6


def _supports_output_schema(variant) -> bool:
    """Return whether a response variant carries a usable output schema."""
    if variant.schema is None or not (variant.media_type or "").lower():
        return False
    media_type = variant.media_type.lower()
    return media_type == "application/json" or media_type.endswith("+json") or media_type.startswith("text/")
