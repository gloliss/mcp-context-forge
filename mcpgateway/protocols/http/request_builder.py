# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/protocols/http/request_builder.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

HTTP request builder (PR2).

Assembles an outbound HTTP request from a tool's ``protocol_config``
(design-document §18) and invocation arguments.  Unlike the legacy adapter,
which mutated a single ``payload`` dict and folded query params into the
body with ``payload.update(query_params)``, this builder keeps path, query,
header, cookie, and body fully independent (design-document §9.6).
"""

# Standard
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# First-Party
from mcpgateway.protocols.codecs.base import CodecContext, EncodedBody
from mcpgateway.protocols.codecs.registry import CodecRegistry

# Methods that never carry a request body.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})

# Locations design-document §9.4 recognises for request parameters.
_PARAM_LOCATIONS = frozenset({"path", "query", "header", "cookie"})


@dataclass
class BuiltRequest:
    """A fully-resolved outbound HTTP request.

    Attributes:
        method: Uppercase HTTP method.
        url_path: The rendered path template (query string excluded).
        query_params: Query-string parameters (independent of the body).
        headers: Header parameters (merged by the caller with auth/global
            headers).
        cookies: Cookie parameters.
        body: Encoded request body, or ``None`` for bodyless methods.
    """

    method: str
    url_path: str
    query_params: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    body: Optional[EncodedBody] = None


class RequestBuilder:
    """Build a ``BuiltRequest`` from arguments and ``protocol_config.request``."""

    def __init__(self, codecs: CodecRegistry) -> None:
        """Initialise with the codec registry used to encode the body.

        Args:
            codecs: The codec registry resolving the configured body codec.
        """
        self._codecs = codecs

    def build(self, arguments: Dict[str, Any], request_config: Optional[Dict[str, Any]]) -> BuiltRequest:
        """Assemble the request from arguments and request configuration.

        Args:
            arguments: The invocation arguments.
            request_config: The ``request`` sub-object of ``protocol_config``
                (method, pathTemplate, preferredContentType, body, and an
                optional ``parameters`` list).

        Returns:
            A fully-resolved ``BuiltRequest``.
        """
        request_config = request_config or {}
        method = str(request_config.get("method") or "GET").upper()
        path_template = str(request_config.get("pathTemplate") or "")

        # 1. Substitute {param} placeholders in the path template and split
        #    the remaining arguments into independent dimensions.
        url_path, remaining = self._render_path(path_template, arguments)
        # ``path_params`` is intentionally unused: ``_render_path`` already
        # substituted the {placeholder}s, so location="path" leftovers from
        # the parameters list have no template slot to fill.
        _path_params, query_params, headers, cookies, body_value = self._split(
            remaining, request_config.get("parameters"), method
        )

        # 2. Encode the body when this method carries one.
        body = None
        if body_value is not None and method not in _BODYLESS_METHODS:
            body = self._encode_body(body_value, request_config, query_params)

        return BuiltRequest(
            method=method,
            url_path=url_path,
            query_params=query_params,
            headers=headers,
            cookies=cookies,
            body=body,
        )

    def _render_path(self, path_template: str, arguments: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Substitute ``{name}`` placeholders, returning path and leftovers.

        Args:
            path_template: The raw path template.
            arguments: The invocation arguments.

        Returns:
            A ``(rendered_path, remaining_arguments)`` tuple.  Placeholder
            values are popped from ``remaining_arguments``; unresolvable
            placeholders are left in place for the caller's validation.
        """
        remaining = dict(arguments)
        rendered = path_template

        def _substitute(match: "re.Match[str]") -> str:
            """Replace one ``{name}`` match with its argument value, if present."""
            name = match.group(1)
            if name in remaining:
                return str(remaining.pop(name))
            return match.group(0)

        if "{" in path_template and "}" in path_template:
            rendered = re.sub(r"\{(\w+)\}", _substitute, path_template)
        return rendered, remaining

    def _split(
        self,
        arguments: Dict[str, Any],
        parameters: Optional[list],
        method: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, str], Dict[str, str], Optional[Any]]:
        """Split remaining arguments into path/query/header/cookie/body.

        Args:
            arguments: Remaining arguments after path substitution.
            parameters: Optional ``parameters`` list from ``request_config``
                (design-document §9.4); each entry is a mapping with
                ``name`` and ``location``.
            method: Uppercase HTTP method.

        Returns:
            A ``(path_params, query_params, headers, cookies, body)`` tuple.
        """
        path_params: Dict[str, Any] = {}
        query_params: Dict[str, Any] = {}
        headers: Dict[str, str] = {}
        cookies: Dict[str, str] = {}
        body: Optional[Any] = None

        if parameters:
            for param in parameters:
                name = param.get("name")
                location = param.get("location")
                if name is None or name not in arguments:
                    continue
                value = arguments[name]
                if location == "path":
                    path_params[name] = value
                elif location == "query":
                    query_params[name] = value
                elif location == "header":
                    headers[name] = str(value)
                elif location == "cookie":
                    cookies[name] = str(value)
            # Every argument not claimed by an explicit path/query/header/cookie
            # parameter becomes the body (§9.6: the dimensions are independent).
            body = {
                k: v
                for k, v in arguments.items()
                if k not in path_params and k not in query_params and k not in headers and k not in cookies
            }
            return path_params, query_params, headers, cookies, body or None

        # No parameters list: route the whole remaining argument set to the
        # query string for bodyless methods, otherwise to the body.
        if method in _BODYLESS_METHODS:
            query_params = dict(arguments)
        else:
            body = dict(arguments)
        return path_params, query_params, headers, cookies, body

    def _encode_body(
        self,
        body_value: Any,
        request_config: Dict[str, Any],
        query_params: Dict[str, Any],
    ) -> EncodedBody:
        """Encode the body using the configured codec.

        Args:
            body_value: The outbound body value.
            request_config: The ``request`` configuration (for codec/media
                type selection).
            query_params: Query params (never merged into the body).

        Returns:
            The encoded ``EncodedBody``.
        """
        del query_params  # query and body remain independent (§9.6)
        body_config = request_config.get("body") or {}
        media_type = body_config.get("mediaType") or request_config.get("preferredContentType")
        codec = self._codecs.resolve(media_type)
        context = CodecContext(preferred_content_type=media_type)
        return codec.encode(body_value, context)
