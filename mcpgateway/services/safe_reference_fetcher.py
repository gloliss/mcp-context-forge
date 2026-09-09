# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/safe_reference_fetcher.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Safe external ``$ref`` fetching and bundling (PR3, design-document §66).

OpenAPI documents may reference sub-documents through external ``$ref``
values (``{"$ref": "https://host/schemas.json"}``).  Fetching those
references requires network access, which must never happen inside the
contract parser (the parser must stay deterministic and network-free) and
must never weaken the platform SSRF posture.  This module therefore:

* fetches each external reference through the existing SSRF pipeline
  (``SecurityValidator.validate_url`` + ``get_isolated_http_client`` with
  ``follow_redirects=False``), streaming with the
  ``mcpgateway_http_spec_max_bytes`` cap;
* refuses redirects outright — the same fail-closed policy as
  ``openapi_service.fetch_openapi_spec``; the manual per-hop redirect loop
  (``protocols.http.redirect.RedirectSecurity``) is a runtime-invocation
  concern and is never applied to spec downloads;
* materialises every external reference into an
  ``x-contextforge-bundled-refs`` array attached to the OpenAPI document,
  rewriting each ``$ref`` to a local
  ``#/x-contextforge-bundled-refs/{index}`` JSON Pointer (index plus the
  original ``#/fragment`` when the reference carried one).

The resulting bundle is self-contained: ``OpenAPIContractProvider`` only
ever resolves local pointers and never touches the network.
"""

# Standard
import copy
import re
from typing import Any, Callable, Iterator, Optional

# Third-Party
import httpx
import orjson
import yaml

# First-Party
from mcpgateway.common.validators import SecurityValidator
from mcpgateway.config import settings
from mcpgateway.services.http_client_service import get_isolated_http_client

# Extension key holding the materialised external reference documents.
BUNDLED_REFS_KEY = "x-contextforge-bundled-refs"

# Maximum number of distinct external references materialised per import.
# Generous for real-world specs, bounded against reference-graph
# amplification attacks.
_MAX_EXTERNAL_REFS = 64

# External references recognised by the bundler: absolute http(s) URLs.
_EXTERNAL_REF_RE = re.compile(r"^https?://", re.IGNORECASE)


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


def _rewrite_refs(node: Any, rewrite: Callable[[str], str]) -> None:
    """Rewrite every ``$ref`` string value inside ``node`` in place.

    Args:
        node: The document subtree to walk.
        rewrite: The pointer-rewriting callback applied to each ``$ref``
            value.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            node["$ref"] = rewrite(ref)
        for value in node.values():
            _rewrite_refs(value, rewrite)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item, rewrite)


class SafeReferenceFetcher:
    """Fetch external ``$ref`` documents through the SSRF pipeline (§66)."""

    def __init__(self, timeout: float = 10.0) -> None:
        """Initialise with the per-request timeout.

        Args:
            timeout: Request timeout in seconds (default: 10.0, matching
                ``openapi_service.fetch_openapi_spec``).
        """
        self.timeout = timeout

    async def fetch(self, url: str) -> bytes:
        """Download one reference document, returning its raw bytes.

        The caller parses the bytes (JSON or YAML); the fetcher stays
        format-agnostic.  Redirects are refused outright: following them
        would require the manual per-hop re-validation loop, which is a
        runtime-invocation concern and never applied to spec downloads.

        Args:
            url: The absolute http(s) URL of the referenced document
                (fragment excluded by the caller).

        Returns:
            The raw response body bytes.

        Raises:
            ValueError: If the URL fails SSRF validation, the response
                redirects, or the body exceeds the configured size cap.
            httpx.HTTPError: If the request fails at the transport level.
        """
        max_bytes = settings.mcpgateway_http_spec_max_bytes

        # SSRF protection: validate before any request is made.
        SecurityValidator.validate_url(url, "HTTP artifact reference URL")

        async with get_isolated_http_client(timeout=self.timeout, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise ValueError(f"HTTP artifact reference redirects are not followed: {url}")

                response.raise_for_status()

                # Early reject via Content-Length when the header is present.
                try:
                    cl = int(response.headers.get("content-length", "0"))
                except (ValueError, OverflowError):
                    cl = 0  # Malformed header — fall through to the streamed check.
                if cl > max_bytes:
                    raise ValueError(f"HTTP artifact reference too large ({cl} bytes, max {max_bytes})")

                # Stream in chunks so we never buffer more than the cap.
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"HTTP artifact reference too large (>{max_bytes} bytes)")
                    chunks.append(chunk)

        return b"".join(chunks)


class ContractArtifactResolver:
    """Materialise external ``$ref`` values into a self-contained bundle (§66)."""

    def __init__(self, fetcher: Optional[SafeReferenceFetcher] = None) -> None:
        """Initialise with a fetcher (injectable for tests).

        Args:
            fetcher: The reference fetcher; defaults to a fresh
                ``SafeReferenceFetcher``.
        """
        self._fetcher = fetcher or SafeReferenceFetcher()

    @staticmethod
    def parse_document(raw: bytes, label: str = "") -> Any:
        """Parse a fetched reference payload (JSON or YAML).

        Args:
            raw: The raw payload bytes.
            label: Human-readable source URL for error messages.

        Returns:
            The parsed document (dict/list/scalar).

        Raises:
            ValueError: If the payload is neither valid JSON nor YAML.
        """
        try:
            return orjson.loads(raw)
        except (orjson.JSONDecodeError, ValueError):
            try:
                return yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise ValueError(f"External $ref payload is not valid JSON or YAML: {label}") from exc

    def collect_external_refs(self, document: Any) -> list[str]:
        """Return the distinct external ``$ref`` URLs in document order.

        Args:
            document: The parsed document to scan.

        Returns:
            The distinct absolute http(s) reference strings (including any
            ``#/fragment``), in first-seen order.
        """
        found: list[str] = []
        seen: set[str] = set()
        for node in _iter_dicts(document):
            ref = node.get("$ref")
            if isinstance(ref, str) and _EXTERNAL_REF_RE.match(ref) and ref not in seen:
                seen.add(ref)
                found.append(ref)
        return found

    @staticmethod
    def _bundle_pointer(base: str, fragment: str, index: dict[str, int]) -> str:
        """Map an external ``$ref`` (base URL + optional fragment) to a bundle pointer.

        Args:
            base: The reference base URL (without fragment).
            fragment: The fragment portion (``""`` when absent).
            index: Base-URL to bundle-index mapping.

        Returns:
            The local JSON Pointer into ``x-contextforge-bundled-refs``.
        """
        pointer = f"#/{BUNDLED_REFS_KEY}/{index[base]}"
        if fragment and fragment != "/":
            pointer += f"/{fragment[1:]}" if fragment.startswith("/") else f"/{fragment}"
        return pointer

    async def resolve_and_bundle(self, document: Any, *, allow_remote: bool = True) -> dict:
        """Materialise external references into a self-contained bundle.

        Walks the reference graph breadth-first: each distinct base URL is
        fetched exactly once, parsed, and stored in the
        ``x-contextforge-bundled-refs`` array.  ``$ref`` values are then
        rewritten to local pointers:

        * ``"https://host/x.json#/Query"`` in the main document becomes
          ``"#/x-contextforge-bundled-refs/0/Query"``;
        * local pointers inside materialised sub-documents are prefixed
          with their bundle index so they resolve against the sub-document
          (``"#/components/schemas/Pet"`` inside index 2 becomes
          ``"#/x-contextforge-bundled-refs/2/components/schemas/Pet"``);
        * nested external references are queued and materialised too.

        Documents without external references are returned as a defensive
        copy unchanged, so callers always receive a bundle dict.

        Args:
            document: The parsed OpenAPI document (must be a mapping).
            allow_remote: When ``False``, any external reference raises.

        Returns:
            The self-contained bundle document.

        Raises:
            ValueError: If ``document`` is not a mapping, ``allow_remote``
                is ``False`` and external references exist, the reference
                count exceeds ``_MAX_EXTERNAL_REFS``, or a fetch/parse
                step fails.
        """
        if not isinstance(document, dict):
            raise ValueError("OpenAPI document must be a JSON object")
        document = copy.deepcopy(document)

        external = self.collect_external_refs(document)
        if not external:
            return document
        if not allow_remote:
            raise ValueError(f"External $ref values are not allowed by policy: {external[0]}")

        # Normalise to base URLs: fragments are client-side pointers and
        # must not participate in fetching or deduplication.
        bases: list[str] = []
        seen_bases: set[str] = set()
        for ref in external:
            base, _sep, _fragment = ref.partition("#")
            if base not in seen_bases:
                seen_bases.add(base)
                bases.append(base)
        if len(bases) > _MAX_EXTERNAL_REFS:
            raise ValueError(f"Too many external $ref values ({len(bases)} > {_MAX_EXTERNAL_REFS})")

        # Breadth-first materialisation: sub-documents may introduce new
        # external references; every base URL is fetched exactly once.
        index: dict[str, int] = {}
        bundled: list[Any] = []
        queue: list[str] = list(bases)
        while queue:
            base = queue.pop(0)
            if base in index:
                continue
            if len(bundled) >= _MAX_EXTERNAL_REFS:
                raise ValueError(f"Too many external $ref values (>{_MAX_EXTERNAL_REFS})")
            try:
                raw = await self._fetcher.fetch(base)
            except httpx.HTTPError as exc:
                raise ValueError(f"Failed to fetch external $ref {base}: {exc}") from exc
            index[base] = len(bundled)
            bundled.append(self.parse_document(raw, base))
            for nested in self.collect_external_refs(bundled[-1]):
                nested_base, _sep, _fragment = nested.partition("#")
                if nested_base not in index and nested_base not in queue:
                    queue.append(nested_base)

        # Rewrite references inside each materialised sub-document:
        # external refs point at the bundled array entry; local pointers
        # are prefixed so they resolve against the sub-document.
        for sub_index, sub_doc in enumerate(bundled):
            def _rewrite_sub(ref: str, _sub_index: int = sub_index) -> str:
                """Map one sub-document ``$ref`` to its bundle pointer."""
                if _EXTERNAL_REF_RE.match(ref):
                    base, _sep, fragment = ref.partition("#")
                    if base not in index:
                        raise ValueError(f"Unresolved external $ref after materialisation: {ref}")
                    return ContractArtifactResolver._bundle_pointer(base, fragment, index)
                return f"#/{BUNDLED_REFS_KEY}/{_sub_index}{ref[1:]}"

            _rewrite_refs(sub_doc, _rewrite_sub)

        # Rewrite external references in the main document.
        def _rewrite_main(ref: str) -> str:
            """Map one main-document ``$ref`` to its bundle pointer."""
            if _EXTERNAL_REF_RE.match(ref):
                base, _sep, fragment = ref.partition("#")
                return ContractArtifactResolver._bundle_pointer(base, fragment, index)
            return ref

        _rewrite_refs(document, _rewrite_main)

        bundle = dict(document)
        bundle[BUNDLED_REFS_KEY] = bundled
        return bundle
