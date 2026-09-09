# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/contracts/openapi.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

OpenAPI contract discovery and runtime validation (PR3, design §15/§22).

``OpenAPIContractProvider`` compiles an immutable OpenAPI 3.x artifact
(external ``$ref`` targets already materialised into
``x-contextforge-bundled-refs`` by ``ContractArtifactService``) into an
``OperationCatalog``:

1. parse the payload (JSON, YAML fallback);
2. whole-document structural validation via ``openapi_spec_validator`` —
   an invalid spec is a hard failure, never an empty catalog;
3. walk the raw document dict to build typed IR (``HttpRequestContract``/
   ``HttpResponseContract``) — openapi-core objects never enter the IR;
4. per-operation failures are skipped with error diagnostics; internal
   ``$ref`` pointers are dereferenced with cycle detection, non-local
   pointers survive as-is with a warning diagnostic.

The module also hosts the framework-free openapi-core shim used for
*request/response runtime validation* (design §22): a plain-dataclass
``Request``/``Response`` pair satisfying the openapi-core protocols, plus
an LRU cache of parsed specs keyed by content hash.  The shim contract is
locked by unit tests because openapi-core has no stable framework-free
datatypes of its own.
"""

# Standard
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from typing import Any, Iterator, Optional

# Third-Party
from openapi_core import OpenAPI
from openapi_core.datatypes import Headers, ImmutableMultiDict, RequestParameters
from openapi_core.validation.exceptions import ValidationError as CoreValidationError
from openapi_spec_validator import validate as validate_openapi_spec
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
import orjson
import yaml

# First-Party
from mcpgateway.protocols.contracts.models import (
    ContractArtifact,
    ContractDiagnostic,
    ContractProviderError,
    DiscoveryContext,
    OperationCatalog,
    OperationDefinition,
)
from mcpgateway.protocols.http.models import (
    HttpBodyVariant,
    HttpParameter,
    HttpRequestContract,
    HttpResponseContract,
    HttpResponseVariant,
)
from mcpgateway.utils.artifact_security import json_pointer_get

# HTTP methods a path item may carry; everything else (``parameters``,
# ``summary``, ``$ref`` ...) is not an operation.
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})

# Parameter locations design §9.4 recognises.
_PARAM_LOCATIONS = frozenset({"path", "query", "header", "cookie"})

# Parsed-spec LRU (risk table §22: at most 16 specs, keyed by content hash).
_SPEC_CACHE_MAX = 16
_spec_cache: OrderedDict[str, OpenAPI] = OrderedDict()


def _media_type_to_codec(media_type: str) -> str:
    """Map an OpenAPI media type to a codec name (design §9.7).

    Args:
        media_type: The raw media type (possibly with parameters).

    Returns:
        One of ``json``/``form``/``multipart``/``text``/``binary``.
    """
    base = media_type.split(";", 1)[0].strip().lower()
    if base == "application/json" or base.endswith("+json"):
        return "json"
    if base == "application/x-www-form-urlencoded":
        return "form"
    if base == "multipart/form-data":
        return "multipart"
    if base.startswith("text/"):
        return "text"
    return "binary"


def _iter_dicts(node: Any) -> Iterator[dict]:
    """Yield every mapping node in a parsed document, depth-first.

    Args:
        node: The document subtree (dict/list/scalar).

    Yields:
        Each nested mapping in document order.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _collect_non_local_refs(document: dict) -> list[str]:
    """Return every ``$ref`` value that is not a local JSON Pointer.

    Args:
        document: The parsed document.

    Returns:
        The distinct non-local reference strings, in first-seen order.
    """
    found: list[str] = []
    seen: set[str] = set()
    for node in _iter_dicts(document):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#") and ref not in seen:
            seen.add(ref)
            found.append(ref)
    return found


def _response_status_sort_key(status: str) -> tuple[int, str]:
    """Order response variants deterministically (2xx first, then others).

    Args:
        status: The response status key (``"200"``, ``"2XX"``, ``"default"``).

    Returns:
        A sort key putting 2xx codes first ascending, then other numeric
        codes, then ``default``, then range and unknown keys.
    """
    if status == "default":
        return (2, "")
    if status.endswith("XX") or status.endswith("xx"):
        return (3, status)
    try:
        code = int(status)
    except ValueError:
        return (4, status)
    if 200 <= code < 300:
        return (0, status)
    return (1, status)


class _RefResolver:
    """Resolve internal ``$ref`` pointers with cycle detection (design §15)."""

    def __init__(self, document: dict) -> None:
        """Initialise with the document the pointers resolve against.

        Args:
            document: The parsed OpenAPI document.
        """
        self._document = document
        self.warnings: list[str] = []

    def resolve(self, node: Any, seen: tuple[str, ...] = ()) -> Any:
        """Dereference a node, following local ``$ref`` chains.

        Args:
            node: The node to resolve (any JSON value).
            seen: Pointer chain already visited, for cycle detection.

        Returns:
            The fully resolved node, or ``None`` when the pointer is
            non-local, unresolvable, or cyclic (a warning is recorded and
            the caller keeps the original node).
        """
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if not isinstance(ref, str):
            return node
        if not ref.startswith("#"):
            self.warnings.append(f"Non-local $ref left unresolved: {ref}")
            return None
        if ref in seen:
            self.warnings.append(f"Circular $ref chain detected: {ref}")
            return None
        try:
            target = json_pointer_get(self._document, ref)
        except (KeyError, ValueError, IndexError) as exc:
            self.warnings.append(f"Unresolvable $ref {ref}: {exc}")
            return None
        return self.resolve(target, seen + (ref,))

    def materialize(self, node: Any, seen: tuple[str, ...] = ()) -> Any:
        """Inline every local ``$ref`` nested anywhere inside ``node``.

        ``resolve`` only dereferences a node that is itself a ``$ref``;
        schemas embedding further references (e.g. an ``items`` entry)
        would otherwise carry dangling pointers into tool input/output
        schemas, which jsonschema validates standalone.  This walk
        returns a self-contained copy of the subtree.

        Args:
            node: The subtree to materialise (dict/list/scalar).
            seen: Pointer chain already visited, for cycle detection.

        Returns:
            A copy of the subtree with local refs inlined.  Circular
            back-edges collapse to a permissive ``{}`` schema and
            non-local/unresolvable refs are kept verbatim (a warning is
            recorded in both cases).
        """
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#"):
                    self.warnings.append(f"Non-local $ref left unresolved: {ref}")
                    return node
                if ref in seen:
                    self.warnings.append(f"Circular $ref chain detected: {ref}")
                    return {}
                try:
                    target = json_pointer_get(self._document, ref)
                except (KeyError, ValueError, IndexError) as exc:
                    self.warnings.append(f"Unresolvable $ref {ref}: {exc}")
                    return node
                return self.materialize(target, seen + (ref,))
            return {key: self.materialize(value, seen) for key, value in node.items()}
        if isinstance(node, list):
            return [self.materialize(item, seen) for item in node]
        return node


class OpenAPIContractProvider:
    """Compile an OpenAPI 3.x artifact into an operation catalog (design §15)."""

    async def discover(self, artifact: ContractArtifact, context: DiscoveryContext) -> OperationCatalog:
        """Compile an immutable artifact into an ``OperationCatalog``.

        Args:
            artifact: The artifact document (external ``$ref`` targets
                already materialised; this provider never touches the
                network).
            context: Discovery context (``max_operations`` limit honoured).

        Returns:
            The compiled catalog.  Per-operation failures surface as
            diagnostics; whole-document failures raise
            ``ContractProviderError``.

        Raises:
            ContractProviderError: If the payload is not parseable, not a
                JSON object, or fails OpenAPI structural validation.
        """
        document = self._parse(artifact.payload)

        # Fail closed on any non-local $ref BEFORE structural validation:
        # the spec validator dereferences references it can fetch — network
        # access must never happen inside the provider (§15), and non-http
        # schemes such as ``file://`` must never reach it either.
        # ContractArtifactService materialises every http(s) ref into a
        # local ``#/x-contextforge-bundled-refs/...`` pointer, so a
        # surviving non-local ref means the artifact was not properly
        # materialised.
        non_local = _collect_non_local_refs(document)
        if non_local:
            raise ContractProviderError(f"OpenAPI artifact contains non-local $ref values (not materialised): {non_local[0]}")

        # Whole-document structural validation: an invalid spec is a hard
        # failure, never an empty catalog (§15).  RecursionError covers
        # cyclic reference graphs, which the validator resolves recursively.
        try:
            validate_openapi_spec(document)
        except (OpenAPIValidationError, RecursionError) as exc:
            raise ContractProviderError(f"OpenAPI specification validation failed: {exc}") from exc

        diagnostics: list[ContractDiagnostic] = []
        operations: list[OperationDefinition] = []
        resolver = _RefResolver(document)
        max_operations = context.limits.get("max_operations")
        seen_keys: set[str] = set()
        truncated = False

        for path, path_item in document.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            if isinstance(path_item.get("$ref"), str):
                resolved_item = resolver.resolve(path_item)
                if not isinstance(resolved_item, dict):
                    continue
                path_item = resolved_item
            path_parameters = path_item.get("parameters")
            if not isinstance(path_parameters, list):
                path_parameters = []
            for method, operation in path_item.items():
                if method not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                if max_operations is not None and len(operations) >= max_operations:
                    truncated = True
                    break
                key = f"{method.upper()} {path}"
                try:
                    operation_def = self._compile_operation(key, path, method, operation, path_parameters, resolver)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    diagnostics.append(
                        ContractDiagnostic(
                            severity="error",
                            code="operation-compilation-failed",
                            message=str(exc),
                            location=f"paths.{path}.{method}",
                            operation_key=key,
                        )
                    )
                    continue
                if key in seen_keys:  # defensive: keys are unique by construction
                    diagnostics.append(
                        ContractDiagnostic(
                            severity="error",
                            code="duplicate-operation-key",
                            message=f"Operation key already present: {key}",
                            location=f"paths.{path}.{method}",
                            operation_key=key,
                        )
                    )
                    continue
                seen_keys.add(key)
                operations.append(operation_def)
            if max_operations is not None and len(operations) >= max_operations:
                truncated = True
                break
        if truncated:
            diagnostics.append(
                ContractDiagnostic(
                    severity="warning",
                    code="operation-limit-reached",
                    message=f"Catalog truncated at max_operations={max_operations}",
                )
            )

        # Reference-resolution warnings become catalog diagnostics.
        for warning in resolver.warnings:
            diagnostics.append(ContractDiagnostic(severity="warning", code="ref-unresolved", message=warning))

        return OperationCatalog(
            source_type="openapi",
            source_hash=hashlib.sha256(artifact.payload).hexdigest(),
            operations=tuple(operations),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _parse(payload: bytes) -> dict:
        """Parse an artifact payload (JSON, YAML fallback).

        Args:
            payload: The raw artifact bytes.

        Returns:
            The parsed document.

        Raises:
            ContractProviderError: If the payload is not parseable or not
                a JSON object.
        """
        try:
            document = orjson.loads(payload)
        except (orjson.JSONDecodeError, ValueError):
            try:
                document = yaml.safe_load(payload)
            except yaml.YAMLError as exc:
                raise ContractProviderError(f"OpenAPI artifact is not valid JSON or YAML: {exc}") from exc
        if not isinstance(document, dict):
            raise ContractProviderError("OpenAPI artifact is not a JSON object document")
        return document

    def _compile_operation(
        self,
        key: str,
        path: str,
        method: str,
        operation: dict,
        path_parameters: list,
        resolver: _RefResolver,
    ) -> OperationDefinition:
        """Compile one path-item operation into an ``OperationDefinition``.

        Args:
            key: The stable operation key (``GET /v1/lots/{lotId}``).
            path: The OpenAPI path template.
            method: The lower-case HTTP method.
            operation: The raw operation mapping.
            path_parameters: The path-item-level ``parameters`` list.
            resolver: The ``$ref`` resolver.

        Returns:
            The compiled operation definition.
        """
        # Merge path-item and operation parameters on (name, in);
        # operation-level entries override path-item entries (§15).
        merged: dict[tuple[str, str], dict] = {}
        for param in path_parameters:
            if not isinstance(param, dict):
                continue
            resolved_param = resolver.resolve(param)
            if isinstance(resolved_param, dict):
                param = resolved_param
            if isinstance(param.get("name"), str) and param.get("in") in _PARAM_LOCATIONS:
                merged[(param["name"], param["in"])] = param
        for param in operation.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            resolved_param = resolver.resolve(param)
            if isinstance(resolved_param, dict):
                param = resolved_param
            if isinstance(param.get("name"), str) and param.get("in") in _PARAM_LOCATIONS:
                merged[(param["name"], param["in"])] = param

        parameters = tuple(self._compile_parameter(param, resolver) for param in merged.values())
        bodies = self._compile_bodies(operation.get("requestBody"), resolver)
        responses = self._compile_responses(operation.get("responses"), resolver)

        return OperationDefinition(
            key=key,
            protocol="http",
            source_operation_id=operation.get("operationId"),
            title=operation.get("summary"),
            description=operation.get("description"),
            deprecated=bool(operation.get("deprecated", False)),
            tags=tuple(operation.get("tags") or ()),
            request=HttpRequestContract(
                method=method.upper(),
                path_template=path,
                parameters=parameters,
                bodies=bodies,
            ),
            response=HttpResponseContract(variants=responses),
        )

    @staticmethod
    def _compile_parameter(param: dict, resolver: _RefResolver) -> HttpParameter:
        """Compile one merged parameter entry.

        Args:
            param: The raw parameter mapping (name/in/schema/...).
            resolver: The ``$ref`` resolver.

        Returns:
            The typed ``HttpParameter``.
        """
        schema_node = param.get("schema")
        schema: dict[str, Any] = {}
        if schema_node is not None:
            resolved = resolver.materialize(schema_node)
            if isinstance(resolved, dict):
                schema = resolved
            elif isinstance(schema_node, dict):
                schema = schema_node  # unresolved ref kept as-is (§15)
        return HttpParameter(
            name=str(param["name"]),
            location=param["in"],
            required=bool(param.get("required", False)),
            schema=schema,
            style=param.get("style"),
            explode=param.get("explode"),
            allow_reserved=bool(param.get("allowReserved", False)),
        )

    @staticmethod
    def _compile_bodies(request_body: Any, resolver: _RefResolver) -> tuple[HttpBodyVariant, ...]:
        """Compile the requestBody content map into body variants.

        Args:
            request_body: The raw ``requestBody`` mapping (or None).
            resolver: The ``$ref`` resolver.

        Returns:
            One variant per media type, in contract order.
        """
        if not isinstance(request_body, dict):
            return ()
        required = bool(request_body.get("required", False))
        content = request_body.get("content") or {}
        variants = []
        for media_type, media_obj in content.items():
            if not isinstance(media_obj, dict):
                continue
            schema_node = media_obj.get("schema")
            schema = None
            schema_ref = None
            if schema_node is not None:
                if isinstance(schema_node, dict) and isinstance(schema_node.get("$ref"), str):
                    schema_ref = schema_node["$ref"]
                resolved = resolver.materialize(schema_node)
                if isinstance(resolved, dict):
                    schema = resolved
                elif isinstance(schema_node, dict):
                    schema = schema_node  # unresolved ref kept as-is (§15)
            variants.append(
                HttpBodyVariant(
                    media_type=media_type,
                    codec=_media_type_to_codec(media_type),
                    schema=schema,
                    required=required,
                    schema_ref=schema_ref,
                )
            )
        return tuple(variants)

    @staticmethod
    def _compile_responses(responses: Any, resolver: _RefResolver) -> tuple[HttpResponseVariant, ...]:
        """Compile the responses map into response variants.

        Args:
            responses: The raw ``responses`` mapping (or None).
            resolver: The ``$ref`` resolver.

        Returns:
            One variant per status/media-type pair (contentless responses
            yield a single variant with an empty media type), ordered
            2xx-first for determinism.
        """
        if not isinstance(responses, dict):
            return ()
        variants = []
        for status in sorted(responses, key=_response_status_sort_key):
            resp_obj = responses[status]
            if not isinstance(resp_obj, dict):
                continue
            description = resp_obj.get("description")
            content = resp_obj.get("content") or {}
            if not content:
                variants.append(HttpResponseVariant(status_code=str(status), media_type="", schema=None, description=description))
                continue
            for media_type, media_obj in content.items():
                if not isinstance(media_obj, dict):
                    continue
                schema_node = media_obj.get("schema")
                schema = None
                if schema_node is not None:
                    resolved = resolver.materialize(schema_node)
                    if isinstance(resolved, dict):
                        schema = resolved
                    elif isinstance(schema_node, dict):
                        schema = schema_node
                variants.append(
                    HttpResponseVariant(
                        status_code=str(status),
                        media_type=media_type,
                        schema=schema,
                        description=description,
                    )
                )
        return tuple(variants)


@dataclass
class OpenAPIRequestShim:
    """Framework-free ``Request`` satisfying openapi-core's protocol.

    Passed to ``validate_openapi_request``/``validate_openapi_response``.
    openapi-core has no stable framework-free request type, so this
    dataclass pins the contract (design §22): plain attributes only,
    ``query``/``cookie`` as ``ImmutableMultiDict``, ``header`` as
    ``Headers``, ``path`` as a plain dict the validator fills from the URL
    match.

    Attributes:
        host_url: URL with scheme and host (e.g. ``https://api.example.com``).
        path: The request path (e.g. ``/pets/5``).
        full_url_pattern: Scheme+host plus the matched path pattern
            (e.g. ``https://api.example.com/pets/{pet_id}``).
        method: Lower-case HTTP method.
        parameters: The ``RequestParameters`` bag.
        content_type: Content type (lower-case, parameters included).
        body: Raw body bytes, or None when absent.
    """

    host_url: str
    path: str
    full_url_pattern: str
    method: str
    parameters: RequestParameters
    content_type: str
    body: Optional[bytes] = None


@dataclass
class OpenAPIResponseShim:
    """Framework-free ``Response`` satisfying openapi-core's protocol.

    Attributes:
        status_code: Integer status code.
        content_type: Content type (lower-case, parameters included).
        headers: Response headers as ``Headers``.
        data: Raw body bytes, or None when absent.
    """

    status_code: int
    content_type: str
    headers: Headers
    data: Optional[bytes] = None


def empty_request_parameters(
    query: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, Any]] = None,
    cookies: Optional[dict[str, Any]] = None,
) -> RequestParameters:
    """Build a ``RequestParameters`` bag with the types openapi-core expects.

    Args:
        query: Query-string values.
        headers: Header values.
        cookies: Cookie values.

    Returns:
        A validator-compatible parameters bag (path starts empty; the
        validator fills it from the URL match).
    """
    return RequestParameters(
        query=ImmutableMultiDict(query or {}),
        header=Headers(headers or {}),
        cookie=ImmutableMultiDict(cookies or {}),
        path={},
    )


def load_openapi(content_hash: str, spec_dict: dict) -> OpenAPI:
    """Return a parsed OpenAPI instance, LRU-cached by content hash.

    The cache is bounded (at most ``_SPEC_CACHE_MAX`` entries, evicting
    least-recently used) because parsed specs are memory-heavy and a
    deployment may hold many artifact versions (risk table §22).

    Args:
        content_hash: The artifact content hash.
        spec_dict: The parsed OpenAPI document.

    Returns:
        The parsed (and cached) ``OpenAPI`` instance.
    """
    cached = _spec_cache.get(content_hash)
    if cached is not None:
        _spec_cache.move_to_end(content_hash)
        return cached
    parsed = OpenAPI.from_dict(spec_dict)
    _spec_cache[content_hash] = parsed
    if len(_spec_cache) > _SPEC_CACHE_MAX:
        _spec_cache.popitem(last=False)
    return parsed


def validate_openapi_request(openapi: OpenAPI, request: OpenAPIRequestShim) -> None:
    """Validate a request shim against the spec, raising on mismatch.

    Args:
        openapi: The parsed spec (see :func:`load_openapi`).
        request: The request to validate.

    Raises:
        ValueError: If openapi-core reports a validation error.
    """
    try:
        openapi.validate_request(request)
    except CoreValidationError as exc:
        raise ValueError(f"Request validation failed: {exc}") from exc


def validate_openapi_response(openapi: OpenAPI, request: OpenAPIRequestShim, response: OpenAPIResponseShim) -> None:
    """Validate a response shim against the spec for the given request.

    Args:
        openapi: The parsed spec (see :func:`load_openapi`).
        request: The request the response answers.
        response: The response to validate.

    Raises:
        ValueError: If openapi-core reports a validation error.
    """
    try:
        openapi.validate_response(request, response)
    except CoreValidationError as exc:
        raise ValueError(f"Response validation failed: {exc}") from exc
